import asyncio
import copy
import json
import logging
import os
import os.path
import pathlib
import shutil
import sys
import time
from abc import abstractmethod
from os import listdir
from os.path import isfile, join

import cbpi
import shortuuid
import yaml
from cbpi.api.dataclasses import Fermenter, FermenterStep, NotificationType, Props, Step
from cbpi.api.persist import atomic_write_json, quarantine
from cbpi.controller.basic_controller2 import BasicController
from cbpi.api.decorator import normalize_action_parameters, resolve_action
from tabulate import tabulate

from ..api.step import (CBPiFermentationStep, CBPiStep, StepMove, StepResult,
                        StepState)


class FermentationController:

    def __init__(self, cbpi):
        self.update_key = "fermenterupdate"
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.path = self.cbpi.config_folder.get_file_path("fermenter_data.json")
        self.data = []
        self.types = {}
        self.steptypes = {}
        # fermenter id -> the step that was RUNNING when the server last
        # stopped. load() turns an active step into STOP so nothing runs before
        # it is asked to - but a step the brewer paused is STOP too, and
        # AutoStart must resume the first and never the second (DEFECTS F2,
        # F4). The step, not just the fermenter: with a paused step ahead of
        # the interrupted one, resuming "the first STOP step" picked the paused
        # one (F7).
        self.interrupted = {}
        # Fermenters whose recipe is being replaced right now (see toggle).
        self._replacing = set()
        # on_shutdown, not on_cleanup. aiohttp runs on_shutdown, then waits up
        # to its shutdown timeout for open requests - the interface's
        # websocket is one, for as long as a browser is open - and only then
        # runs on_cleanup. systemd's SIGKILL arrives first, so fermenter
        # actors were never switched off on a stop (DEFECTS F22).
        self.cbpi.app.on_shutdown.append(self.shutdown)

    async def init(self):
        logging.info("INIT Fermentation Controller")
        self.check_fermenter_file()
        await self.load()
        pass

    def check_fermenter_file(self):
        if (
            os.path.exists(self.cbpi.config_folder.get_file_path("fermenter_data.json"))
            is False
        ):
            logging.warning("Missing fermenter_data.json file. INIT empty file")
            data = {"data": []}
            destfile = self.cbpi.config_folder.get_file_path("fermenter_data.json")
            with open(destfile, "w") as f:
                json.dump(data, f, indent=4, sort_keys=True)

        pathlib.Path(self.cbpi.config_folder.get_file_path("fermenterrecipes")).mkdir(
            parents=True, exist_ok=True
        )

    async def _deenergize(self, fermenter):
        """Switch a fermenter's heater, cooler and valve off.

        shutdown() stopped the step tasks and stopped there, so a fermenter
        that was heating or cooling when it was shut down stayed that way.
        Cancelling the task that was commanding an actor does not command the
        actor off - nothing else was going to.

        This is the same defect that ActorController.shutdown fixes for
        kettles, in a different controller, and it matters most on the
        single-fermenter path: ActorController.shutdown sweeps every actor, but
        it only runs when the whole application exits. Shutting down one
        fermenter runs none of it, so its element stayed live with no step
        behind it.

        Each actor is switched off independently. One failing must not stop the
        others, because the remaining ones are exactly the elements still
        energized.
        """
        for role in ("heater", "cooler", "valve"):
            actor_id = getattr(fermenter, role, None)
            if not actor_id:
                continue
            try:
                await self.cbpi.actor.off(actor_id)
            except Exception as e:  # noqa: BLE001
                self.logger.error(
                    "Failed to switch off %s %s for fermenter %s: %s",
                    role, actor_id, getattr(fermenter, "name", "?"), e,
                )

    async def _stop_logic(self, fermenter):
        """Stop a fermenter's temperature logic, so nothing re-energizes what is switched off next."""
        instance = getattr(fermenter, "instance", None)
        if instance is None:
            return
        try:
            await instance.stop()
        except Exception as e:  # noqa: BLE001 - the actors are switched off regardless
            self.logger.error(
                "Failed to stop the logic of fermenter %s: %s",
                getattr(fermenter, "name", "?"), e,
            )

    async def _stop_steps(self, fermenter):
        for step in fermenter.steps:
            try:
                self.logger.info("Stop {}".format(step.name))
                step.instance.shutdown = True
                await step.instance.stop()
            except Exception as e:
                self.logger.error(e)

    async def shutdown(self, app=None, fermenterid=None):
        """Stop fermenters: every one when the server stops, or one (`fermenterid`).

        One fermenter is shut down only to load a new recipe onto it. That
        stops its steps and deliberately LEAVES its temperature logic running:
        the beer in the tank still needs holding while the profile is
        replaced, and with AutoMode No on every step nothing else would ever
        switch control back on. A step on AutoMode Yes switches the logic off
        as it stops, so that is refused while the recipe is replaced, and the
        logic restarted if it stopped anyway (DEFECTS F44). Its actors are
        switched off only if no logic is driving them - otherwise the logic
        owns them, and switching them off just had it switch them back on a
        second later (DEFECTS F25).
        """
        try:
            self.save()
        except Exception as e:  # noqa: BLE001
            # Now an on_shutdown hook: aiohttp stops at the first one that
            # raises, so a full SD card here would skip every stop after it.
            self.logger.error("Could not save fermenters while shutting down: %s", e)
        if fermenterid == None:
            for fermenter in self.data:
                self.logger.info("Shutdown {}".format(fermenter.name))
                # The logic first. A logic still ticking re-commanded the
                # cooler a second after _deenergize switched it off, and the
                # process then exited with it on (DEFECTS F10).
                await self._stop_logic(fermenter)
                await self._stop_steps(fermenter)
                # After the steps, so a step cannot re-command an actor this
                # has just switched off.
                await self._deenergize(fermenter)
        else:
            fermenter = self._find_by_id(fermenterid)
            self.logger.info("Shutdown {}".format(fermenter.name))
            was_alive = self._logic_alive_for(fermenter)
            await self._stop_steps(fermenter)
            if was_alive and not self._logic_alive_for(fermenter):
                # Stopped by a step's own cleanup, some way toggle() did not
                # see. It was holding the beer, and nothing else would switch
                # it on again (DEFECTS F44).
                self.logger.warning("%s: temperature control stopped while its recipe was "
                                    "replaced - switching it on again", fermenter.name)
                await self.start_logic(fermenterid)
            if not self._logic_alive_for(fermenter):
                await self._deenergize(fermenter)

    def _logic_alive_for(self, fermenter):
        instance = getattr(fermenter, "instance", None)
        return instance is not None and self._logic_alive(instance)

    async def load(self):
        data, problem = self._read(self.path)
        if problem is not None:
            # Kept, not deleted: it is the only copy of every fermenter and
            # profile, and it was deleted and replaced with an empty one -
            # silently - so an unattended tank came back from a power cut with
            # no fermenter and nothing controlling it (DEFECTS F38).
            kept = quarantine(self.path)
            data, backup_problem = self._read(self.path + self.BACKUP_SUFFIX)
            if backup_problem is None:
                self._alarm("fermenter_data.json could not be read ({}). Restored the copy "
                            "saved with the last change; the unreadable file is kept as {}."
                            .format(problem, kept))
            else:
                data = {"data": []}
                self._alarm("fermenter_data.json could not be read ({}), and neither could "
                            "its backup ({}). Starting with no fermenters; the unreadable "
                            "file is kept as {}.".format(problem, backup_problem, kept))
            atomic_write_json(self.path, data, sort_keys=True)
        left_out = []
        for entry in data["data"]:
            fermenter = self._create(entry)
            if fermenter is None:
                left_out.append(entry)
            else:
                self.data.append(fermenter)
        if left_out:
            # The next save would drop them for good: the file as it was is kept.
            kept = self._copy_aside(self.path)
            names = ", ".join(str(e.get("name", e.get("id", "?")) if isinstance(e, dict) else e)
                              for e in left_out)
            self._alarm("{} fermenter(s) could not be loaded and are left out: {}. The file "
                        "as it was is kept as {}.".format(len(left_out), names, kept))
        elif problem is None:
            self._write_backup(data)

    # The last fermenter_data.json saved, kept beside it.
    BACKUP_SUFFIX = ".bak"

    @staticmethod
    def _read(path):
        """(data, None) for a readable fermenter file, else (None, why not)."""
        try:
            with open(path) as json_file:
                data = json.load(json_file)
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                return None, "no fermenter list in it"
            return data, None
        except FileNotFoundError:
            return None, "missing"
        except Exception as e:  # noqa: BLE001
            return None, "{}: {}".format(type(e).__name__, e)

    def _write_backup(self, data):
        try:
            atomic_write_json(self.path + self.BACKUP_SUFFIX, data, sort_keys=True)
        except Exception as e:  # noqa: BLE001 - the backup must never break a save
            self.logger.warning("Could not write the fermenter backup: %s", e)

    @staticmethod
    def _copy_aside(path):
        target = "{}.unloadable-{}".format(path, time.strftime("%Y%m%d-%H%M%S"))
        try:
            shutil.copy2(path, target)
            return target
        except OSError:
            return None

    def _alarm(self, message):
        self.logger.error(message)
        try:
            self.cbpi.notify("Fermenters", message, NotificationType.ERROR)
        except Exception:  # noqa: BLE001
            pass

    def _create_step(self, fermenter, item):
        id = item.get("id")
        name = item.get("name")
        props = Props(item.get("props"))
        try:
            endtime = int(item.get("endtime", 0))
        except (TypeError, ValueError):
            endtime = 0

        status = StepState(item.get("status", "I"))
        if status == StepState.ACTIVE:
            status = StepState("S")
            self.interrupted[getattr(fermenter, "id", None)] = id
        type = item.get("type")

        try:
            type_cfg = self.steptypes.get(type)
            clazz = type_cfg.get("class")
            instance = clazz(self.cbpi, fermenter, item, props, self._done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s" % (id, e))
            instance = None

        step = FermenterStep(
            id=id,
            name=name,
            fermenter=fermenter,
            props=props,
            type=type,
            status=status,
            endtime=endtime,
            instance=instance,
        )
        return step

    def _done(self, step_instance, result, fermenter):
        logging.info(result)
        step_instance.step["status"] = "D"
        self.save()
        if result == StepResult.NEXT:
            asyncio.create_task(self.start(fermenter))

    def _create(self, data):
        try:
            id = data.get("id")
            name = data.get("name")
            sensor = data.get("sensor")
            pressure_sensor = data.get("pressure_sensor")
            heater = data.get("heater")
            cooler = data.get("cooler")
            valve = data.get("valve", "")
            logictype = data.get("type")
            temp = data.get("target_temp")
            pressure = data.get("target_pressure")
            brewname = data.get("brewname")
            description = data.get("description")
            props = Props(data.get("props", {}))
            fermenter = Fermenter(
                id,
                name,
                sensor,
                pressure_sensor,
                heater,
                cooler,
                valve,
                brewname,
                description,
                props,
                temp,
                pressure,
                logictype,
            )
            fermenter.steps = list(
                map(
                    lambda item: self._create_step(fermenter, item),
                    data.get("steps", []),
                )
            )
            self.push_update()
            return fermenter
        except Exception:
            return

    def _find_by_id(self, id):
        return next((item for item in self.data if item.id == id), None)

    async def get_all(self):
        return list(map(lambda x: x.to_dict(), self.data))

    def get_types(self):
        result = {}
        for key, value in self.types.items():
            result[key] = dict(
                name=value.get("name"),
                properties=value.get("properties"),
                actions=value.get("actions"),
            )
        return result

    def get_steptypes(self):
        result = {}
        for key, value in self.steptypes.items():
            result[key] = dict(
                name=value.get("name"),
                properties=value.get("properties"),
                actions=value.get("actions"),
            )
        return result

    def get_state(self):
        if self.data == []:
            pass

        return {
            "data": list(map(lambda x: x.to_dict(), self.data)),
            "types": self.get_types(),
            "steptypes": self.get_steptypes(),
        }

    def get_step_state(self, fermenterid=None):
        if self.data == []:
            pass
        fermentersteps = []
        steplist = list(map(lambda x: x.to_dict(), self.data))
        for fermenter in steplist:
            if fermenterid == fermenter.get("id"):
                fermentersteps = {
                    "id": fermenter.get("id"),
                    "steps": fermenter.get("steps"),
                }
        return fermentersteps

    def get_fermenter_steps(self):
        if self.data == []:
            pass
        fermentersteps = []
        steplist = list(map(lambda x: x.to_dict(), self.data))
        for fermenter in steplist:
            fermenterstep = {"id": fermenter.get("id"), "steps": fermenter.get("steps")}
            fermentersteps.append(fermenterstep)
        return fermentersteps

    async def find_step_by_id(self, id):
        actionstep = None
        for item in self.data:
            step = self._find_step_by_id(item.steps, id)
            if step is not None:
                actionstep = step
        return actionstep

    async def get(self, id: str):
        return self._find_by_id(id)

    async def create(self, data: Fermenter):
        data.id = shortuuid.uuid()
        self.data.append(data)
        self.save()
        self.push_update()
        return data

    async def update(self, item: Fermenter):

        def _update(old_item: Fermenter, item: Fermenter):
            old_item.name = item.name
            old_item.sensor = item.sensor
            old_item.pressure_sensor = item.pressure_sensor
            old_item.heater = item.heater
            old_item.cooler = item.cooler
            old_item.valve = item.valve
            old_item.type = item.type
            old_item.brewname = item.brewname
            old_item.description = item.description
            old_item.props = item.props
            old_item.target_temp = item.target_temp
            old_item.target_pressure = item.target_pressure
            return old_item

        self.data = list(
            map(lambda old: _update(old, item) if old.id == item.id else old, self.data)
        )
        self.save()
        self.push_update()
        return item

    async def set_target_temp(self, id: str, target_temp):
        try:
            item = self._find_by_id(id)
            logging.info(item.target_temp)
            if item:
                item.target_temp = target_temp
                self.save()
                self.push_update()
        except Exception as e:
            logging.error("Failed to set Target Temp {} {}".format(id, e))

    async def set_target_pressure(self, id: str, target_pressure):
        try:
            item = self._find_by_id(id)
            logging.info(item.target_pressure)
            if item:
                item.target_pressure = target_pressure
                self.save()
                self.push_update()
        except Exception as e:
            logging.error("Failed to set Target Pressure {} {}".format(id, e))

    async def delete(self, id: str):
        item = self._find_by_id(id)
        if item is not None:
            # Stop everything driving its actors before forgetting it. Deleting
            # a running fermenter used to leave its logic going - with nothing
            # in the interface to switch it off - commanding the heater and
            # cooler of a fermenter that no longer existed (DEFECTS F10).
            await self._stop_logic(item)
            await self._stop_steps(item)
            await self._deenergize(item)
        self.data = list(filter(lambda item: item.id != id, self.data))
        self.save()
        self.push_update()

    def save(self):
        data = dict(data=list(map(lambda item: item.to_dict(), self.data)))
        atomic_write_json(self.path, data, sort_keys=True)
        self._write_backup(data)

    def create_step(self, id, item):
        try:
            stepid = shortuuid.uuid()
            item["id"] = stepid
            status = StepState("I")
            type = item.get("type")
            name = item.get("name")
            endtime = item.get("endtime", 0)
            props = Props(item.get("props"))
            fermenter = self._find_by_id(id)
            try:
                type_cfg = self.steptypes.get(type)
                clazz = type_cfg.get("class")
                instance = clazz(self.cbpi, fermenter, item, props, self._done)
            except Exception as e:
                logging.warning("Failed to create step instance %s - %s" % (id, e))
                instance = None
            step = FermenterStep(
                id=stepid,
                name=name,
                fermenter=fermenter,
                props=props,
                type=type,
                status=status,
                endtime=endtime,
                instance=instance,
            )

            return step
        except Exception as e:
            self.logger.error(e)

    async def update_step(self, id, item):
        fermenter = self._find_by_id(id)
        stepid = item.get("id")
        props = item.get("props")
        status = StepState("I")
        type = item.get("type")
        endtime = 0
        name = item.get("name")
        props = Props(item.get("props"))

        logging.info("update step")
        try:
            type_cfg = self.steptypes.get(type)
            logging.info(type_cfg)
            clazz = type_cfg.get("class")
            logging.info(clazz)
            instance = clazz(self.cbpi, fermenter, item, props, self._done)
            logging.info(instance)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s " % (item.id, e))
            instance = None
        step = FermenterStep(
            id=stepid,
            name=name,
            fermenter=fermenter,
            props=props,
            type=type,
            status=status,
            endtime=endtime,
            instance=instance,
        )

        try:
            fermenter.steps = list(
                map(lambda old: step if old.id == step.id else old, fermenter.steps)
            )
        except Exception as e:
            logging.info(e)

        self.save()

        self.push_update("fermenterstepupdate")

    async def delete_step(self, id, stepid):
        item = self._find_by_id(id)
        # might require later check if step is active
        item.steps = list(filter(lambda item: item.id != stepid, item.steps))
        self.save()
        self.push_update("fermenterstepupdate")

    async def clearsteps(self, id):
        item = self._find_by_id(id)
        # might require later check if step is active
        item.steps = []
        item.brewname = ""
        self.push_update()
        self.save()
        self.push_update("fermenterstepupdate")

    async def update_endtime(self, id, stepid, endtime):
        try:
            item = self._find_by_id(id)
            step = self._find_step_by_id(item.steps, stepid)
            step.endtime = int(endtime)
            self.save()
            self.push_update("fermenterstepupdate")
        except Exception as e:
            self.logger.error(e)

    def _find_by_status(self, data, status):
        return next((item for item in data if item.status == status), None)

    def _find_step_by_id(self, data, id):
        return next((item for item in data if item.id == id), None)

    async def update_endtime(self, id, stepid, endtime):
        try:
            item = self._find_by_id(id)
            step = self._find_step_by_id(item.steps, stepid)
            step.endtime = int(endtime)
            self.save()
            self.push_update("fermenterstepupdate")
        except Exception as e:
            self.logger.error(e)

    async def start(self, id):
        self.logger.info("Start {}".format(id))
        try:
            item = self._find_by_id(id)

            step = self._find_by_status(item.steps, StepState.ACTIVE)
            if step is not None:
                logging.error("Steps already running")
                return

            step = self._find_by_status(item.steps, StepState.STOP)
            if step is not None:
                endtime = step.endtime
                await step.instance.start()
                logging.info("Restarting step {}".format(step.name))
                if endtime != 0:
                    logging.info("Need to change timer")
                step.status = StepState.ACTIVE
                self.save()
                self.push_update()
                self.push_update("fermenterstepupdate")
                return

            step = self._find_by_status(item.steps, StepState.INITIAL)
            logging.info(step)
            if step is None:
                self.logger.info("No futher step to start")
            else:
                step.instance.endtime = 0
                await step.instance.start()
                logging.info("Starting step {}".format(step.name))
                step.status = StepState.ACTIVE
                self.save()
                self.push_update()
                self.push_update("fermenterstepupdate")

        except Exception as e:
            self.logger.error(e)

    async def stop(self, id):
        try:
            item = self._find_by_id(id)
            step = self._find_by_status(item.steps, StepState.ACTIVE)
            # logging.info(step)
            # logging.info(step.status)
            if step != None:
                logging.info("CALLING STOP STEP")
                try:
                    await step.instance.stop()
                    step.status = StepState.STOP
                    self.save()
                except Exception as e:
                    logging.error("Failed to stop fermenterstep - Id: %s" % step.id)
                self.push_update()
                self.push_update("fermenterstepupdate")

        except Exception as e:
            self.logger.error(e)

    @staticmethod
    def _logic_alive(instance):
        """Is this logic still in charge - running, or waiting to restart itself?"""
        task = getattr(instance, "task", None)
        if task is not None:
            return not task.done()
        return getattr(instance, "running", False) is True

    async def start_logic(self, id):
        item = None
        try:
            item = self._find_by_id(id)
            if item is None:
                logging.warning("Cannot start the logic of unknown fermenter %s", id)
                return
            logging.info("{} Start Id {} ".format(item.name, id))
            # The task, not the `running` flag. A logic waiting out its restart
            # delay has `running` False and a live task, and a second instance
            # started beside it would drive the same actors (DEFECTS F8).
            if item.instance is not None and self._logic_alive(item.instance):
                logging.warning("{} already running {}".format(item.name, id))
                return
            if item.type is None:
                logging.warning("{} No Type {}".format(item.name, id))
                return
            clazz = self.types[item.type]["class"]
            item.instance = clazz(self.cbpi, item.id, item.props)

            await item.instance.start()
            item.instance.running = True
            item.instance.task = asyncio.create_task(
                item.instance._run()
            )

            logging.info("{} started {}".format(item.name, id))

        except Exception as e:
            logging.error("{} Cant start {} - {}".format(getattr(item, "name", id), id, e))

    async def toggle(self, id):

        try:
            item = self._find_by_id(id)

            if item.instance is None or item.instance.state == False:
                await self.start_logic(id)
            elif id in getattr(self, "_replacing", ()):
                # A step stopping because its recipe is being replaced. With
                # AutoMode Yes its on_stop switches control off - and with the
                # new recipe's steps on AutoMode No, nothing ever switched it
                # on again: no control, and nobody told (DEFECTS F44).
                logging.info("%s: recipe being replaced - temperature control stays on",
                             item.name)
            else:
                await item.instance.stop()
            self.push_update()

        except Exception as e:
            logging.error("Failed to switch on FermenterLogic {} {}".format(id, e))

    async def next(self, id):
        self.logger.info("Next {} ".format(id))
        try:
            item = self._find_by_id(id)
            logging.info(item)
            step = self._find_by_status(item.steps, StepState.ACTIVE)
            logging.info(step)
            if step is not None:
                if step.instance is not None:
                    step.status = StepState.DONE
                    await step.instance.next()

            step = self._find_by_status(item.steps, StepState.STOP)
            logging.info(step)
            if step is not None:
                if step.instance is not None:
                    logging.info(step)
                    step.status = StepState.DONE
                    logging.info(step)
                    self.save()
                    await self.start(id)
            else:
                logging.info("No Step is running")
            self.push_update()
            self.push_update("fermenterstepupdate")

        except Exception as e:
            self.logger.error(e)

    async def reset(self, id):
        self.logger.info("Reset")
        try:
            item = self._find_by_id(id)
            for step in item.steps:
                self.logger.info("Stopping Step {} {}".format(step.name, step.id))
                try:
                    await step.instance.stop()
                    await step.instance.reset()
                    step.status = StepState.INITIAL
                    step.endtime = 0
                except Exception as e:
                    self.logger.error(e)
            self.save()
            self.push_update()
            self.push_update("fermenterstepupdate")

        except Exception as e:
            self.logger.error(e)

    async def move_step(self, fermenter_id, step_id, direction):
        try:
            fermenter = self._find_by_id(fermenter_id)
            index = next(
                (i for i, item in enumerate(fermenter.steps) if item.id == step_id),
                None,
            )
            if index == None:
                return
            if index == 0 and direction == -1:
                return
            if index == len(fermenter.steps) - 1 and direction == 1:
                return

            fermenter.steps[index], fermenter.steps[index + direction] = (
                fermenter.steps[index + direction],
                fermenter.steps[index],
            )
            self.save()
            self.push_update()
            self.push_update("fermenterstepupdate")

        except Exception as e:
            self.logger.error(e)

    def remove_key(self, d, key):
        r = dict(d)
        del r[key]
        return r

    def push_update(self, key="fermenterupdate"):

        if key == self.update_key:
            self.cbpi.ws.send(
                dict(topic=key, data=list(map(lambda item: item.to_dict(), self.data)))
            )

            for item in self.data:
                fermenters = self.remove_key(item.to_dict(), "steps")
                self.cbpi.push_update(
                    "cbpi/{}/{}".format(self.update_key, item.id), fermenters
                )
            pass
        else:
            fermentersteps = self.get_fermenter_steps()
            self.cbpi.ws.send(dict(topic=key, data=fermentersteps))

            # send mqtt update for active femrentersteps
            for fermenter in fermentersteps:
                active = False
                for step in fermenter["steps"]:
                    if step["status"] == "A":
                        active = True
                        active_step = step
                #                        self.cbpi.push_update("cbpi/{}/{}/{}".format(key,fermenter['id'],step['id']), step)
                # else:
                #    self.cbpi.push_update("cbpi/{}/{}".format(key,fermenter['id']), "")
                if active:
                    self.cbpi.push_update(
                        "cbpi/{}/{}".format(key, fermenter["id"]), active_step
                    )
                else:
                    self.cbpi.push_update("cbpi/{}/{}".format(key, fermenter["id"]), "")

    async def call_action(self, id, action, parameter) -> None:
        logging.info("FermenterStep Controller - call Action {} {}".format(id, action))
        try:
            item = await self.find_step_by_id(id)
            if item is None:
                logging.error("FermenterStep Controller - no such step %s", id)
                return
            method = resolve_action(item.instance, action)
            if method is None:
                logging.error(
                    "FermenterStep Controller - refused undeclared action %r on "
                    "%s - only methods decorated with @action can be called",
                    action,
                    id,
                )
                return
            await method(**normalize_action_parameters(parameter))
        except Exception as e:
            logging.error(
                "FermenterStep Controller - Failed to call action on {} {} {}".format(
                    id, action, e
                )
            )

    async def savetobook(self, fermenterid):
        name = shortuuid.uuid()
        path = self.cbpi.config_folder.get_fermenter_recipe_by_id(name)
        fermenter = self._find_by_id(fermenterid)
        try:
            brewname = fermenter.brewname
            description = fermenter.description

        except:
            brewname = ""
            description = ""
        self.basic_data = {"name": brewname, "description": description}

        try:
            fermentersteps = fermenter.steps
        except:
            fermentersteps = []
        data = dict(
            basic=self.basic_data,
            steps=list(map(lambda item: item.to_dict(), fermentersteps)),
        )
        with open(path, "w") as file:
            yaml.dump(data, file)

    async def load_recipe(self, data, fermenterid, name):
        replacing = self.__dict__.setdefault("_replacing", set())
        replacing.add(fermenterid)
        try:
            await self.shutdown(None, fermenterid)
        except:
            pass
        finally:
            replacing.discard(fermenterid)
        fermenter = self._find_by_id(fermenterid)

        def add_runtime_data(item):
            item["status"] = "I"
            item["endtime"] = 0
            item["id"] = shortuuid.uuid()
            item["props"]["Sensor"] = fermenter.sensor

        list(map(lambda item: add_runtime_data(item), data.get("steps")))
        try:
            fermenter.description = data["basic"].get("desc")
        except:
            fermenter.description = "No Description"
        if name is not None:
            fermenter.brewname = name
        else:
            try:
                fermenter.brewname = data["basic"].get("name")
            except:
                fermenter.brewname = "Fermentation"
        await self.update(fermenter)
        fermenter.steps = []
        for item in data.get("steps"):
            fermenter.steps.append(self.create_step(fermenterid, item))

        self.save()
        self.push_update("fermenterstepupdate")

    async def add_step(self, fermenterid, newstep):
        fermenter = self._find_by_id(fermenterid)
        step = self.create_step(fermenterid, newstep)
        fermenter.steps.append(step)
        self.save()
        self.push_update("fermenterstepupdate")
        return step
