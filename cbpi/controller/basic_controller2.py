import asyncio
import json
import logging
import os
import os.path
import sys

import shortuuid
from cbpi.api.dataclasses import Actor, Fermenter, NotificationType, Props
from cbpi.api.persist import atomic_write_json, quarantine
from cbpi.api.decorator import normalize_action_parameters, resolve_action
from tabulate import tabulate


class BasicController:

    def __init__(self, cbpi, resource, file):
        self.resource = resource
        self.update_key = ""
        self.sorting = False
        self.name = self.__class__.__name__
        self.cbpi = cbpi
        self.cbpi.register(self)
        self.service = self
        self.types = {}
        self.logger = logging.getLogger(__name__)
        self.data = []
        self.autostart = True
        self.path = self.cbpi.config_folder.get_file_path(file)
        self.cbpi.app.on_cleanup.append(self.shutdown)

    async def init(self):
        await self.load()

    def create(self, data):
        return self.resource(
            data.get("id"),
            data.get("name"),
            type=data.get("type"),
            props=Props(data.get("props", {})),
        )

    async def load(self):
        try:
            logging.info("{} Load ".format(self.name))
            with open(self.path) as json_file:
                data = json.load(json_file)
                data["data"].sort(key=lambda x: x.get("name").upper())

                for i in data["data"]:
                    self.data.append(self.create(i))

                if self.autostart is True:
                    for item in self.data:
                        logging.info("{} Starting ".format(self.name))
                        await self.start(item.id)
                    await self.push_udpate()
        except Exception as e:
            # logging.error(e)
            kept = quarantine(self.path)
            logging.warning(
                "Invalid %s file - starting empty. The unreadable file was kept "
                "at %s", self.path, kept or "(could not be preserved)"
            )
            atomic_write_json(self.path, dict(data=[]), sort_keys=True)

            with open(self.path) as json_file:
                data = json.load(json_file)
                data["data"].sort(key=lambda x: x.get("name").upper())

                for i in data["data"]:
                    self.data.append(self.create(i))

                if self.autostart is True:
                    for item in self.data:
                        logging.info("{} Starting ".format(self.name))
                        await self.start(item.id)
                    await self.push_udpate()

    async def save(self):
        logging.info("{} Save ".format(self.name))
        data = dict(data=list(map(lambda actor: actor.to_dict(), self.data)))
        atomic_write_json(self.path, data, sort_keys=True)
        await self.push_udpate()

    async def push_udpate(self):
        self.cbpi.ws.send(
            dict(
                topic=self.update_key,
                data=list(map(lambda item: item.to_dict(), self.data)),
            ),
            self.sorting,
        )
        # self.cbpi.push_update("cbpi/{}".format(self.update_key), list(map(lambda item: item.to_dict(), self.data)))
        for item in self.data:
            self.cbpi.push_update(
                "cbpi/{}/{}".format(self.update_key, item.id), item.to_dict()
            )

    def find_by_id(self, id):
        return next((item for item in self.data if item.id == id), None)

    def get_index_by_id(self, id):
        return next((i for i, item in enumerate(self.data) if item.id == id), None)

    async def shutdown(self, app=None):
        logging.info("{} Shutdown ".format(self.name))
        tasks = []
        for item in self.data:
            if item.instance is not None and item.instance.running is True:
                item.instance.task.cancel()
                tasks.append(item.instance.task)
        # return_exceptions is required: gathering cancelled tasks re-raises
        # CancelledError, which aborted this coroutine before save() below ever
        # ran - so nothing was persisted on a normal shutdown, and any caller
        # doing further cleanup was skipped too.
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.save()

    async def stop(self, id):
        logging.info("{} Stop Id {} ".format(self.name, id))
        try:
            item = self.find_by_id(id)
            if item is None or item.instance is None:
                return
            instance = item.instance

            # Order matters: ask the loop to finish, then give the driver its
            # own hook, then cancel and join whatever is left.
            instance.running = False
            # The driver hook is advisory and must not be able to skip the
            # cancellation below. It is `pass` on every shipped driver, but a
            # plugin's can raise - and if that propagated, the pulse loop would
            # be left running by the very call meant to stop it.
            try:
                await instance.stop()
            except Exception as e:  # noqa: BLE001
                logging.error(
                    "%s driver stop hook failed for %s: %s", self.name, id, e
                )

            # Cancelling is what actually stops it.
            #
            # This set running=False and returned. For a GPIO actor that flag
            # is only read at the top of its pulse loop, and the loop spends
            # its time inside `await asyncio.sleep(heating_time)` with the pin
            # HIGH - so at full duty the element stayed energized for the rest
            # of the period, up to the whole sample time, after stop() had
            # returned and the interface said it was off.
            #
            # delete() and update() both call this and then replace or discard
            # the instance, so the abandoned task also outlived the registry
            # entry: an old driver still holding a pin while a new one is
            # created for it.
            #
            # CBPiActor._run() switches the output off in a finally, so
            # cancelling is what guarantees the pin goes low - and awaiting it
            # is what makes that true before this returns rather than
            # eventually.
            task = getattr(instance, "task", None)
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            await self.push_udpate()
        except Exception as e:
            logging.error("{} Cant stop {} - {}".format(self.name, id, e))

    async def start(self, id):
        logging.info("{} Start Id {} ".format(self.name, id))
        try:
            item = self.find_by_id(id)
            if item.instance is not None and item.instance.running is True:
                logging.warning("{} already running {}".format(self.name, id))
                return
            if item.type is None:
                logging.warning("{} No Type {}".format(self.name, id))
                return
            clazz = self.types[item.type]["class"]
            item.instance = clazz(self.cbpi, item.id, item.props)

            await item.instance.start()
            item.instance.running = True
            item.instance.task = asyncio.create_task(
                item.instance._run()
            )

            logging.info("{} started {}".format(self.name, id))

        except Exception as e:
            line = "{} Cant start {} - {}".format(self.name, id, e)
            logging.error(line)
            self.cbpi.notify("Error", line, NotificationType.ERROR)

    def get_types(self):
        result = {}
        for key, value in self.types.items():
            result[key] = dict(
                name=value.get("name"),
                properties=value.get("properties"),
                actions=value.get("actions"),
            )
        return result

    def get_state(self):
        return {
            "data": list(map(lambda x: x.to_dict(), self.data)),
            "types": self.get_types(),
        }

    async def add(self, item):
        logging.info("{} Add".format(self.name))
        item.id = shortuuid.uuid()
        self.data.append(item)
        if self.autostart is True:
            await self.start(item.id)
        await self.save()
        return item

    async def update(self, item):
        logging.info("{} Get Update".format(self.name))
        await self.stop(item.id)

        self.data = list(
            map(
                lambda old_item: item if old_item.id == item.id else old_item, self.data
            )
        )
        if self.autostart is True:
            await self.start(item.id)
        await self.save()
        return self.find_by_id(item.id)

    async def delete(self, id) -> None:
        logging.info("{} Delete".format(self.name))
        await self.stop(id)
        self.data = list(filter(lambda x: x.id != id, self.data))
        await self.save()

    def _transient_instance(self, item, action):
        """A logic instance for configuring a stopped item, or None.

        Built only when the named action declares `allow_stopped=True`, and
        only for configuration: it is constructed and discarded, never started,
        so `running` stays False and `_run()` never launches. An action reached
        this way can write properties; it has no loop from which to drive an
        actor.

        Resolved from the registered class rather than from any instance, so
        the opt-in cannot be faked by something in an instance dictionary.
        """
        try:
            if item.type is None:
                return None
            registered = self.types.get(item.type)
            if not registered:
                return None
            clazz = registered["class"]
            declared = getattr(clazz, action, None) if isinstance(action, str) else None
            if declared is None or not getattr(declared, "action", False):
                return None
            if not getattr(declared, "allow_stopped", False):
                return None
            return clazz(self.cbpi, item.id, item.props)
        except Exception as e:  # noqa: BLE001 - never fail a request on this
            logging.warning(
                "%s could not prepare %r for a stopped %s: %s",
                self.name, action, item.id, e,
            )
            return None

    async def call_action(self, id, action, parameter) -> None:
        logging.info("{} call all Action {} {}".format(self.name, id, action))
        try:
            item = self.find_by_id(id)
            if item is None:
                logging.error("%s no such item %s", self.name, id)
                return False
            if item.instance is None:
                # No running logic. Some actions are still legitimate.
                #
                # The distinction is commanding versus configuring. Most
                # actions command something and genuinely need a running
                # instance. But an action that only writes a property the
                # logic reads on its next pass has nothing to do with whether
                # it is running, and refusing it makes the setting unreachable
                # exactly when a brewer would naturally reach for it - setting
                # the boil power before starting the boil.
                #
                # Opt-in per action via @action(..., allow_stopped=True), so
                # nothing that was previously rejected starts running by
                # accident. The instance built here is never started: running
                # stays False and its control loop never launches, so it can
                # configure but not command.
                transient = self._transient_instance(item, action)
                if transient is not None:
                    method = resolve_action(transient, action)
                    if method is not None:
                        await method(**normalize_action_parameters(parameter))
                        await self.save()
                        await self.push_udpate()
                        return True

                # Not a rejected action - there is nothing to dispatch to.
                #
                # Kettles have autostart False, so no logic instance exists
                # until the brewer starts one. An action sent before that used
                # to fall through to the allowlist check below and be reported
                # as "undeclared action ... only methods decorated with
                # @action", which blames the plugin author for a decorator that
                # is present and correct.
                #
                # Observed on the rig: the brewer set a boil power from the
                # dashboard before starting the logic, got HTTP 204 and no
                # message, and the value did nothing. The log then sent whoever
                # read it looking for a missing decorator.
                logging.error(
                    "%s cannot run action %r on %s: its logic is not running. "
                    "Start the kettle first.",
                    self.name,
                    action,
                    id,
                )
                # Tell the brewer, not just the log.
                #
                # The endpoint answers 204 either way, so a dashboard that
                # sends an action to a stopped logic shows nothing at all: the
                # dialog closes, the value appears to have been accepted, and
                # nothing happens. Silence is the worst possible answer here,
                # because the brewer goes on believing the element is about to
                # do what was asked.
                try:
                    from cbpi.api.dataclasses import NotificationType

                    self.cbpi.notify(
                        getattr(item, "name", self.name),
                        "Start the logic before setting this - {} is not "
                        "running, so the change was not applied.".format(
                            getattr(item, "name", "it")
                        ),
                        NotificationType.WARNING,
                    )
                except Exception:  # noqa: BLE001 - reporting, never fatal
                    pass
                return False
            method = resolve_action(item.instance, action)
            if method is None:
                logging.error(
                    "%s refused undeclared action %r on %s - only methods "
                    "decorated with @action can be called",
                    self.name,
                    action,
                    id,
                )
                return False
            await method(**normalize_action_parameters(parameter))
            return True
        except Exception as e:
            logging.error(
                "{} Failed to call action on {} {} {}".format(self.name, id, action, e)
            )
            return False
