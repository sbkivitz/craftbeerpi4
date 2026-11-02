import asyncio
import copy
import inspect
import json
import logging
import math
import os
import os.path
import subprocess
import sys
import time
from datetime import datetime

from cbpi.api.persist import atomic_write_json, quarantine
from os import listdir
from os.path import isfile, join

import cbpi
import shortuuid
import yaml
from cbpi.api import clock
from cbpi.api.dataclasses import (NotificationAction, NotificationType, Props,
                                  Step)
from tabulate import tabulate

from ..api.decorator import normalize_action_parameters, resolve_action
from ..api.step import CBPiStep, StepMove, StepResult, StepState


def system_clock_synchronized():
    """Whether the system clock has been set from a time server since boot.

    True or False where the system can say - systemd's timedatectl, as on a
    Raspberry Pi; None where it cannot (Windows, no timedatectl), and there a
    PC's battery-backed clock keeps time through a power cut anyway.

    A Raspberry Pi has no battery-backed clock. After a power cut it boots
    with the time it last saved (fake-hwclock), which can be hours behind,
    and the right time comes from the network a minute or so later. Anything
    that decides by the time of day right after a boot must wait for it.
    """
    if not sys.platform.startswith("linux"):
        return None
    try:
        out = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = (out.stdout or "").strip().lower()
    if out.returncode != 0 or value not in ("yes", "no"):
        return None
    return value == "yes"


class StepController:

    # Carrying on by itself after a restart (allow_resume). Real seconds: the
    # first is how long to wait for the system clock to be confirmed before
    # deciding by it, the second how long to wait for every probe's first
    # reading - a step that plans from a reading, as BrewStart does, would
    # otherwise plan from nothing in the first second after a boot.
    RESUME_CLOCK_WAIT = 600
    RESUME_CLOCK_POLL = 5
    RESUME_SENSOR_WAIT = 30

    # What a step the profile is holding for the brewer shows in the mash
    # profile and on the dashboard while it waits (_hold_for_brewer).
    HOLD_SUMMARY = "Waiting for you to start it"

    # The step the profile is holding until the brewer starts it, or None. A
    # class default as well as set in __init__: a controller assembled without
    # __init__, as test_mashin_holds_temp.py builds one, crashed in reset_all.
    _held_step_id = None

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.path = self.cbpi.config_folder.get_file_path("step_data.json")
        self.basic_data = {}
        self.step = None
        self.types = {}
        self._resume_after_startup = None
        self._resume_task = None
        # The step the profile is holding before until the brewer starts it
        # (_hold_for_brewer), or None.
        self._held_step_id = None
        self.cbpi.app.on_cleanup.append(self.shutdown)

    async def init(self):
        logging.info("INIT STEP Controller")
        self.load(startActive=True)
        if self._resume_after_startup is not None:
            # Not now: the actors and kettles a step switches on are set up
            # after this controller (CraftBeerPi.init_serivces). The app's
            # start-up hooks run once everything is.
            self.cbpi.app.on_startup.append(self._start_resume)

    def _recover_interrupted_step(self):
        """Offer to resume a profile interrupted by a restart, rather than guessing.

        A step persisted as ACTIVE has no task behind it after a reload, because a
        task cannot be serialised. Left alone that state is unrecoverable from the
        UI: next() raised on task.cancel() against None and returned HTTP 500,
        start() refused with "Steps already running", and reset_all() declines to
        reset while a step is ACTIVE.

        The lookup that was meant to handle this passed the raw string "A". Since
        StepState is a plain Enum, StepState.ACTIVE == "A" is False, so it never
        matched and the intent never took effect.

        Restarting the step automatically would be the wrong repair. A fresh
        instance re-runs on_start(), which may re-enable AutoMode and switch a
        heater on with nobody present - which also contradicts the deliberate
        choice in ActorController.create() not to restore actor state. And the
        brewer, not the software, is the one who knows whether the mash is still
        worth continuing after however long the power was out.

        So the step is demoted to STOP and the brewer is asked. STOP is a state the
        existing machinery already understands: start() resumes from it and next()
        skips past it. Elapsed time is recorded by CBPiStep during the run, so
        resuming continues from where it stopped instead of restarting a sixty
        minute rest from zero.

        Unless the step was allowed to carry on by itself (allow_resume) and the
        time it was allowed until has not passed. Scott, 30 Oct: "BrewStart
        should resume after an overnight power cut and attempt to honor the
        contract to have hot water ready for me". BrewStart allows it for
        itself and for the step it hands over to, until two hours after the
        time it was asked for; nothing else does. Such a step is still demoted
        here, and picked up again once start-up has finished, the clock has been
        confirmed and the probes have read (_resume_interrupted).
        """
        interrupted = self.find_by_status(StepState.ACTIVE)
        if interrupted is None:
            return

        elapsed = self._elapsed_of(interrupted)
        interrupted.status = StepState.STOP

        until = self._resume_until(interrupted)
        # Checked again before carrying on, once the clock can be trusted. A Pi
        # boots with a clock that is behind, never ahead, so a time already
        # passed by it has certainly passed.
        if until is not None and until > clock.now():
            logging.warning(
                "Step '%s' was active when the server stopped. It may carry on by "
                "itself until %s, and will once start-up is complete; %ss of it "
                "had run.",
                interrupted.name, self._clock_time(until), elapsed,
            )
            self._resume_after_startup = interrupted.id
            return

        logging.warning(
            "Step '%s' was active when the server stopped. Marking it stopped; "
            "%ss of it had run.",
            interrupted.name,
            elapsed,
        )
        self._ask_about_interrupted(
            interrupted,
            None if until is None else
            "It could carry on by itself only until {}, so it waits for you.".format(
                self._clock_time(until)),
        )

    @staticmethod
    def _elapsed_of(step):
        try:
            return int(float(step.props.get(CBPiStep.ELAPSED_PROP, 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _ask_about_interrupted(self, interrupted, reason=None):
        """The "Resume brew?" question, with why it is asked when that is news."""
        elapsed = self._elapsed_of(interrupted)
        if elapsed > 0:
            detail = (
                "About {} of it had already run, and that is remembered - resuming "
                "continues from there rather than starting the step again.".format(
                    self._format_duration(elapsed)
                )
            )
        else:
            detail = "It had not started timing yet, so resuming runs it in full."
        if reason:
            detail = reason + " " + detail

        try:
            self.cbpi.notify(
                "Resume brew?",
                "'{}' was interrupted when the server stopped. {} Check your kettle "
                "first, then choose.".format(interrupted.name, detail),
                NotificationType.WARNING,
                action=[
                    NotificationAction("Resume", self.resume),
                    NotificationAction("Skip this step", self.next),
                ],
            )
        except Exception as e:
            logging.warning("Could not ask about the interrupted step: %s", e)

    @staticmethod
    def _resume_until(step):
        """Brewing-clock seconds until which `step` may carry on by itself after a
        restart, or None if it was never allowed to."""
        try:
            until = float(step.props.get(CBPiStep.RESUME_UNTIL_PROP, None))
        except (TypeError, ValueError):
            return None
        return until if math.isfinite(until) else None

    @staticmethod
    def _clock_time(seconds):
        """'07:00' - or 'Sat 07:00' on another day - on the brewing clock."""
        try:
            moment = datetime.fromtimestamp(float(seconds))
            today = datetime.fromtimestamp(clock.now()).date()
        except (TypeError, ValueError, OverflowError, OSError):
            return "?"
        return moment.strftime("%H:%M" if moment.date() == today else "%a %H:%M")

    async def allow_resume(self, step_id, until, by=None):
        """Let a step carry on by itself if a restart interrupts it before `until`.

        `until` is in seconds on the brewing clock (cbpi.api.clock.now()), `by`
        the name of the step that allows it, said to the brewer when it
        happens. For a step that has promised something by a time - BrewStart's
        hot water by 07:00 - and for the step it hands that promise to.
        Everything else that was running when the server stopped waits for the
        brewer, as it always has. A reset forgets it.

        True when the step was found and marked.
        """
        step = self.find_by_id(step_id)
        try:
            until = float(until)
        except (TypeError, ValueError):
            return False
        if step is None or not math.isfinite(until):
            return False
        step.props[CBPiStep.RESUME_UNTIL_PROP] = round(until, 1)
        step.props[CBPiStep.RESUME_BY_PROP] = str(by or "")
        await self.save()
        return True

    async def _start_resume(self, app=None):
        # A task of its own: start-up must not wait on the clock or the probes.
        self._resume_task = asyncio.create_task(self._resume_interrupted())

    async def _resume_interrupted(self):
        """Carry on with the step interrupted by the restart, if it still may."""
        step_id, self._resume_after_startup = self._resume_after_startup, None
        try:
            trusted = await self._wait_for_trusted_clock(self.RESUME_CLOCK_WAIT)
            await self._wait_for_first_readings(self.RESUME_SENSOR_WAIT)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never decide on a half-made check
            logging.warning("Could not check before carrying on: %s", e)
            trusted = False

        step = self.find_by_id(step_id)
        # The brewer may have answered in the meantime: started, skipped, reset.
        if step is None or step.status != StepState.STOP:
            return
        if self.find_by_status(StepState.ACTIVE) is not None:
            return

        until = self._resume_until(step)
        if until is None:
            self._ask_about_interrupted(step)
            return
        if trusted is False:
            self._ask_about_interrupted(
                step,
                "It could carry on by itself until {}, but the server could not "
                "confirm the time of day - its clock is set from the internet "
                "after a power cut - so it waits for you.".format(
                    self._clock_time(until)),
            )
            return
        if until <= clock.now():
            self._ask_about_interrupted(
                step,
                "It could carry on by itself only until {}, so it waits for "
                "you.".format(self._clock_time(until)),
            )
            return

        logging.warning("Step '%s' is carrying on by itself after the restart.", step.name)
        await self.start_step(step)
        await self.save()
        if step.status != StepState.ACTIVE:
            return  # start_step has told the brewer why
        by = step.props.get(CBPiStep.RESUME_BY_PROP, None)
        allowed = (
            "'{}' allows that until {}".format(by, self._clock_time(until))
            if by and by != step.name
            else "which it may do until {}".format(self._clock_time(until))
        )
        try:
            self.cbpi.notify(
                "Carried on after a restart",
                "'{}' was running when the server stopped. The server is back, "
                "and '{}' has carried on by itself - {}. Stop the profile if "
                "that is not wanted.".format(step.name, step.name, allowed),
                NotificationType.WARNING,
                action=[NotificationAction("OK")],
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not report carrying on: %s", e)

    async def _wait_for_trusted_clock(self, timeout):
        """True once the system clock is confirmed, None where it cannot be told,
        False if it is not confirmed within `timeout` real seconds."""
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + timeout
        while True:
            synced = await loop.run_in_executor(None, system_clock_synchronized)
            if synced is not False:
                return synced
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.RESUME_CLOCK_POLL)

    async def _wait_for_first_readings(self, timeout):
        """Until every sensor has read once, or `timeout` real seconds."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sensors = getattr(getattr(self.cbpi, "sensor", None), "data", None) or []
            if all(getattr(s, "instance", None) is None
                   or getattr(s.instance, "last_update", None) is not None
                   for s in sensors):
                return
            await asyncio.sleep(0.5)

    @staticmethod
    def _format_duration(seconds):
        minutes, secs = divmod(int(seconds), 60)
        if minutes and secs:
            return "{}m {}s".format(minutes, secs)
        if minutes:
            return "{} minutes".format(minutes)
        return "{} seconds".format(secs)

    def create(self, data):

        id = data.get("id")
        name = data.get("name")
        type = data.get("type")
        status = StepState(data.get("status", "I"))
        props = Props(data.get("props", {}))

        try:
            type_cfg = self.types.get(type)
            clazz = type_cfg.get("class")
            instance = clazz(self.cbpi, id, name, props, self.done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s" % (id, e))
            instance = None
        step = Step(id, name, type=type, status=status, instance=instance, props=props)
        return step

    def load(self, startActive=False):

        # create file if not exists
        if os.path.exists(self.path) is False:
            logging.warning("Missing step_data.json file. INIT empty file")
            with open(self.path, "w") as file:
                json.dump(dict(basic={}, steps=[]), file, indent=4, sort_keys=True)

        # load from json file
        try:
            with open(self.path) as json_file:
                data = json.load(json_file)
                self.basic_data = data["basic"]
                self.profile = data["steps"]

            # Start step after start up
            self.profile = list(map(lambda item: self.create(item), self.profile))
            if startActive is True:
                self._recover_interrupted_step()
                self._recover_hold()

        except:
            kept = quarantine(self.path)
            logging.warning(
                "Invalid step_data.json file - starting empty. The unreadable "
                "file was kept at %s", kept or "(could not be preserved)"
            )
            atomic_write_json(
                self.path, dict(basic={"name": ""}, steps=[]), sort_keys=True
            )

            with open(self.path) as json_file:
                data = json.load(json_file)
                self.basic_data = data["basic"]
                self.profile = data["steps"]

            # Start step after start up
            self.profile = list(map(lambda item: self.create(item), self.profile))
            if startActive is True:
                # Unreachable in practice: this branch has just deleted the file and
                # recreated it with steps=[], so there is nothing to recover. Kept
                # consistent with the path above rather than left to rot.
                self._recover_interrupted_step()

    async def add(self, item: Step):
        logging.debug("Add step")
        item.id = shortuuid.uuid()
        item.status = StepState.INITIAL
        try:
            type_cfg = self.types.get(item.type)
            clazz = type_cfg.get("class")
            item.instance = clazz(self.cbpi, item.id, item.name, item.props, self.done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s " % (id, e))
            item.instance = None
        self.profile.append(item)
        await self.save()
        await self._props_saved(item)
        return item

    async def _props_saved(self, item):
        """Let the step act on settings the brewer has just saved.

        See CBPiStep.on_props_saved. A hook that fails is logged, and the save
        it follows stands: the settings are on disk either way.
        """
        hook = getattr(item.instance, "on_props_saved", None)
        if hook is None:
            return
        try:
            result = hook()
            if inspect.isawaitable(result):
                await result
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "Step '%s' could not act on its saved settings: %s", item.name, e
            )

    # Told to the brewer when a running step's settings are not changed.
    EDIT_WHILE_RUNNING = (
        "'{}' is running, so its settings were not changed. Press Stop, make "
        "the change, then Start: it carries on with the time it had left."
    )

    async def update(self, item: Step):
        """Replace a step's settings. Returns the step as saved, or None if refused.

        Not while it runs (DEFECTS M33, HC-CORE-01). This built a new instance
        and swapped it in without stopping the running one, which went on
        outside the profile: Stop found nothing active, an import could not
        reach it, and its kettle logic - the HLT element and the pump - kept
        going. Seen on 2 Nov at 100% after both.
        """

        logging.info("update step")
        existing = self.find_by_id(item.id)
        if existing is not None and existing.status == StepState.ACTIVE:
            logging.warning("Refused to change step '%s' while it runs", existing.name)
            try:
                self.cbpi.notify(
                    existing.name,
                    self.EDIT_WHILE_RUNNING.format(existing.name),
                    NotificationType.WARNING,
                    action=[NotificationAction("OK")],
                )
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not say why the change was refused: %s", e)
            return None
        if existing is not None:
            # An edit changes a step's settings, not how far the profile has
            # got with it. The endpoint builds every step as INITIAL, so a
            # finished step went back in the queue - and start() takes the
            # first INITIAL, so it ran again after the step in hand.
            item.status = existing.status
            # Runtime bookkeeping - underscored props: the time a stopped rest
            # has run, the hop reminders already given, a mash-in waiting for
            # its malt - belongs to the run, not the form. The step form sends
            # back the copy it loaded when it opened, which can be minutes old:
            # taken, it would give a reminder again or run time again (DEFECTS
            # M37). So the run's own values always win; the form's are dropped.
            if item.props is None:
                item.props = Props()
            incoming = item.props.to_dict() if hasattr(item.props, "to_dict") \
                else dict(item.props)
            for key in [k for k in incoming if str(k).startswith("_")]:
                del item.props[key]
            previous = existing.props.to_dict() if hasattr(existing.props, "to_dict") \
                else dict(existing.props or {})
            for key, value in previous.items():
                if str(key).startswith("_"):
                    item.props[key] = value
        try:
            type_cfg = self.types.get(item.type)
            clazz = type_cfg.get("class")
            item.instance = clazz(self.cbpi, item.id, item.name, item.props, self.done)
        except Exception as e:
            logging.warning("Failed to create step instance %s - %s " % (item.id, e))
            item.instance = None

        if item.instance is not None and item.id == self._held_step_id:
            # Still held for the brewer: the new instance must say so too.
            item.instance.summary = self.HOLD_SUMMARY

        self.profile = list(
            map(lambda old: item if old.id == item.id else old, self.profile)
        )
        await self.save()
        await self._props_saved(item)
        return item

    async def save(self):
        logging.debug("save profile")
        data = dict(
            basic=self.basic_data,
            steps=list(map(lambda item: item.to_dict(), self.profile)),
        )
        atomic_write_json(self.path, data, sort_keys=True)
        self.push_udpate()

    @staticmethod
    def _brewer_wait(step):
        """(message, button) if `step` must not be started by the profile moving
        on by itself, else None.

        A step says so with wait_for_brewer() - BoilStep does, unless
        CONFIRM_BEFORE_BOIL is No. One that raises is held: not knowing whether
        a step may heat unattended is not permission to.
        """
        instance = getattr(step, "instance", None)
        check = getattr(instance, "wait_for_brewer", None)
        if not callable(check):
            return None
        try:
            wait = check()
        except Exception as e:  # noqa: BLE001
            logging.warning(
                "Step '%s' could not say whether it may start by itself, so it "
                "waits for the brewer: %s", step.name, e,
            )
            return ("'{}' is waiting for you to start it.".format(step.name), "Start")
        if not wait:
            return None
        try:
            message, button = wait
        except (TypeError, ValueError):
            message, button = wait, "Start"
        return (str(message), str(button or "Start"))

    async def _held_instead(self, step, by_brewer):
        """Hold before `step` instead of starting it, when it waits for the
        brewer and the brewer is not the one starting it. True if held."""
        if by_brewer:
            return False
        wait = self._brewer_wait(step)
        if not wait:
            return False
        self._hold_for_brewer(step, wait)
        await self.save()
        self.push_udpate(complete=True)
        return True

    def _hold_for_brewer(self, step, wait):
        """Leave `step` unstarted and ask the brewer to start it.

        Scott, 1 Nov, at the boil on the simulator: "I need to be able to use
        the dashboard to operate the pump. Right now, my only option is this
        dialog box ... I think it should require a manual advance to boil step
        instead of auto-advance". The boil step used to start by itself the
        moment the mash out ended, hold its element off, and ask "Is the wort
        in the boil kettle?" - in a dialog over the whole interface, with the
        wort pump that moves the wort behind it.

        Now the profile stops before the step. Nothing is active, so nothing
        heats and the dashboard is the brewer's. The step shows that it is
        waiting, and a notification says why, with a button that starts it;
        so do Start and Next. Asked again after a restart (_recover_hold).
        """
        message, button = wait
        self._held_step_id = step.id
        if step.instance is not None:
            step.instance.summary = self.HOLD_SUMMARY
        logging.info("Holding before '%s' until the brewer starts it", step.name)
        held_id = step.id

        async def start_held():
            await self._start_held(held_id)

        try:
            self.cbpi.notify(
                step.name, message, NotificationType.WARNING,
                action=[NotificationAction(button, start_held)],
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not ask the brewer to start '%s': %s", step.name, e)

    async def _start_held(self, step_id):
        """The button on a hold: start the step it was raised for, as the brewer.

        Only that step, and only while the profile still waits before it. A
        notification stays clickable after the profile has moved on (see
        BoilStep.confirm_transfer), and an old button must not start whatever
        step happens to come next.
        """
        if (self.find_by_status(StepState.ACTIVE) is not None
                or self.find_by_status(StepState.STOP) is not None):
            logging.info("Ignoring an old start button: the profile has moved on")
            return
        waiting = self.find_by_status(StepState.INITIAL)
        if waiting is None or waiting.id != step_id:
            logging.info("Ignoring an old start button for step %s", step_id)
            return
        await self.start()

    def _recover_hold(self):
        """After a restart while the profile waited for the brewer before a step,
        ask again: the reminder and its button did not survive the restart."""
        if (self.find_by_status(StepState.ACTIVE) is not None
                or self.find_by_status(StepState.STOP) is not None):
            return
        if self.find_by_status(StepState.DONE) is None:
            return  # not begun, so nothing is waiting
        waiting = self.find_by_status(StepState.INITIAL)
        if waiting is None:
            return
        wait = self._brewer_wait(waiting)
        if wait:
            self._hold_for_brewer(waiting, wait)

    def _clear_hold(self):
        """Forget a hold - the profile was reset, cleared or replaced."""
        held = self.find_by_id(self._held_step_id) if self._held_step_id else None
        if (held is not None and held.instance is not None
                and getattr(held.instance, "summary", "") == self.HOLD_SUMMARY):
            held.instance.summary = ""
        self._held_step_id = None

    async def start(self, by_brewer=True):
        """Start the next step - resuming a stopped one first.

        `by_brewer` is True when the brewer asked (Start, Resume, Next on a
        profile with nothing running, the button on a hold), and False when the
        profile is moving on by itself: a step finished, or one was skipped. A
        step that waits for the brewer (_brewer_wait - a boil) is then held
        rather than started, so it never begins heating unattended.
        """

        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Steps already running")
            return

        # A failed step is where the profile stopped, and the brewer's Start
        # retries it - what the failure notice tells them to do. Until
        # 4.7.7.post24 Start passed over it to the next step not yet started,
        # as the brewer, so after a failed mash out the boil began heating at
        # once with no question (DEFECTS M32). Moving on by itself, the profile
        # never passes a failed step.
        failed = self.find_by_status(StepState.ERROR)
        if failed is not None:
            if not by_brewer:
                logging.warning(
                    "Step '%s' failed: the profile waits for the brewer", failed.name
                )
                return
            logging.info("Retry failed step %s", failed.name)
            await self.start_step(failed, by_brewer=True)
            await self.save()
            return

        step = self.find_by_status(StepState.STOP)
        if step is not None:
            if await self._held_instead(step, by_brewer):
                return
            logging.info("Resume step")
            self.cbpi.push_update(
                topic="cbpi/notification",
                data=dict(type="info", title="Resume", message="Calling resume step"),
            )
            await self.start_step(step, by_brewer=by_brewer)
            await self.save()
            return

        step = self.find_by_status(StepState.INITIAL)
        if step is not None:
            if await self._held_instead(step, by_brewer):
                return
            logging.info("Start Step")
            self.cbpi.push_update(
                topic="cbpi/notification",
                data=dict(type="info", title="Start", message="Calling start step"),
            )
            self.push_udpate(complete=True)
            await self.start_step(step, by_brewer=by_brewer)
            await self.save()
            return
        self.cbpi.notify(
            "Brewing Complete",
            "Now the yeast will take over",
            action=[NotificationAction("OK")],
        )
        self.cbpi.push_update(
            topic="cbpi/notification",
            data=dict(
                type="info",
                title="Brewing completed",
                message="Now the yeast will take over",
            ),
        )
        logging.info("BREWING COMPLETE")

    async def previous(self):
        logging.info("Trigger Previous")

    async def next(self):
        logging.info("Trigger Next")
        logging.debug("Current profile: %s", self.profile)
        active = self.find_by_status(StepState.ACTIVE)
        step = active
        if step is not None:
            if step.instance is not None and getattr(step.instance, "task", None):
                await step.instance.next()
            else:
                # An ACTIVE step with no task behind it is a violated invariant,
                # normally left by a restart. Returning success here without
                # changing anything would be worse than the old crash: the brewer
                # sees the button work while the profile stays stuck, and start()
                # and reset() both remain blocked by the ACTIVE status.
                #
                # Demoting to STOP is a real transition the rest of the machinery
                # understands, and the STOP branch below then advances normally.
                logging.warning(
                    "Step '%s' is active with no running task - recovering it to "
                    "stopped so the profile can move on.",
                    step.name,
                )
                step.status = StepState.STOP
                await self.save()

        # A failed step is skipped like a stopped one (DEFECTS M32). It used to
        # fall through to the start() below, as the brewer, which passed over
        # it and began the next step - a boil, heating with no question.
        step = self.find_by_status(StepState.STOP) or self.find_by_status(StepState.ERROR)
        if step is not None:
            if step.instance is not None:
                step.status = StepState.DONE
                await self.save()
                # The step after a skipped one starts as the profile would start
                # it, not as the brewer starting it: skipping an interrupted mash
                # out ("Resume brew?" - Skip this step) says nothing about the
                # boil kettle, which may still be empty.
                await self.start(by_brewer=False)
        elif active is None and self.find_by_status(StepState.INITIAL) is not None:
            # Nothing running and nothing stopped: the profile is waiting before
            # its next step - held for the brewer before a boil, or not begun.
            # Next means go on, so start it.
            await self.start()
        else:
            logging.info("No Step is running")

    async def resume(self):
        # Dead twice over before: find_by_status("P") never matched - StepState is a
        # plain Enum so the string comparison fails, and there is no PAUSE member
        # anyway - and had it matched, step.get("instance") would have raised, since
        # Step is a dataclass with no .get().
        #
        # Delegating to start() rather than calling start_step() directly keeps one
        # guarded path: start() refuses while another step is ACTIVE, notifies, and
        # persists the new status. Doing it here as well would duplicate that badly.
        await self.start()

    async def stop(self):
        step = self.find_by_status(StepState.ACTIVE)
        if step != None:
            logging.info("CALLING STOP STEP")
            try:
                await step.instance.stop()
                self.cbpi.push_update(
                    topic="cbpi/notification",
                    data=dict(type="info", title="Pause", message="Calling paue step"),
                )
                step.status = StepState.STOP

                await self.save()
            except Exception as e:
                logging.error("Failed to stop step - Id: %s" % step.id)

    async def reset_all(self):
        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Please stop before reset")
            return

        for item in self.profile:
            logging.info("Reset %s" % item)
            item.status = StepState.INITIAL
            # Recorded progress is only meaningful for a step that was interrupted
            # mid-run. Reset means starting over, so leaving it behind would build
            # the next timer from what remained: a sixty minute rest interrupted at
            # minute forty-five would come back as a fifteen minute rest, silently.
            # Cleared on the persisted record as well as the instance, because a
            # step whose instance failed to construct still has props on disk.
            try:
                if item.props is not None and CBPiStep.ELAPSED_PROP in item.props:
                    del item.props[CBPiStep.ELAPSED_PROP]
            except Exception as e:
                logging.warning("Could not clear progress for %s: %s", item.id, e)
                # Deleting is the tidy way, but the point is that no elapsed
                # time survives a reset - so if the container will not let it
                # be removed, overwrite it. Failing to do either is how a
                # sixty minute rest silently becomes a fifteen minute one.
                try:
                    item.props[CBPiStep.ELAPSED_PROP] = 0
                except Exception as e2:  # noqa: BLE001
                    logging.error(
                        "Could not reset progress for %s either: %s", item.id, e2
                    )
            # And permission to carry on by itself after a restart: that was
            # given for one brew's promise, and a reset starts another.
            for key in (CBPiStep.RESUME_UNTIL_PROP, CBPiStep.RESUME_BY_PROP):
                try:
                    if item.props is not None and key in item.props:
                        del item.props[key]
                except Exception as e:  # noqa: BLE001
                    logging.warning("Could not clear %s for %s: %s", key, item.id, e)
                    try:
                        item.props[key] = ""   # reads as never allowed
                    except Exception:  # noqa: BLE001
                        pass
            # And everything else a step kept about this brew - underscored by
            # convention, like the two above: the mash-in waiting for its malt,
            # the boil reminders already given (DEFECTS M35, M36). Left behind,
            # the next brew's mash-in would ask for malt in cold water.
            try:
                props = item.props.to_dict() if hasattr(item.props, "to_dict") \
                    else dict(item.props or {})
                for key in [k for k in props if str(k).startswith("_")]:
                    del item.props[key]
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not clear what %s kept: %s", item.id, e)
            try:
                await item.instance.reset()
                if hasattr(item.instance, "clear_progress"):
                    item.instance.clear_progress()
                self.cbpi.push_update(
                    topic="cbpi/notification",
                    data=dict(type="info", title="Stop", message="Calling stop step"),
                )
            except:
                logging.warning("No Step Instance - Id: %s", item.id)
        self._clear_hold()
        await self.save()
        self.push_udpate()

    def get_types(self):
        result = {}
        for key, value in self.types.items():
            # if "ferment" not in str(value.get("class")).lower():
            result[key] = dict(
                name=value.get("name"),
                properties=value.get("properties"),
                actions=value.get("actions"),
            )
        return result

    def get_state(self):
        return {
            "basic": self.basic_data,
            "steps": list(map(lambda item: item.to_dict(), self.profile)),
            "types": self.get_types(),
        }

    async def move(self, id, direction: StepMove):
        index = self.get_index_by_id(id)
        if direction not in [-1, 1]:
            self.logger.error("Cant move. Direction 1 and -1 allowed")
            return
        self.profile[index], self.profile[index + direction] = (
            self.profile[index + direction],
            self.profile[index],
        )
        await self.save()
        self.push_udpate()

    async def delete(self, id):
        step = self.find_by_id(id)

        if step is None:
            logging.error("Cant find step - Nothing deleted - Id: %s", id)
            return

        if step.status == StepState.ACTIVE:
            logging.error("Cant delete active Step %s", id)
            return

        self.profile = list(filter(lambda item: item.id != id, self.profile))
        await self.save()

    async def shutdown(self, app=None):
        logging.info("Mash Profile Shutdown")
        for p in self.profile:
            instance = p.instance
            # Stopping all running task
            if (
                hasattr(instance, "task")
                and instance.task != None
                and instance.task.done() is False
            ):
                logging.info("Stop Step")
                await instance.stop()
                await instance.task
        await self.save()
        self.push_udpate()

    def done(self, step, result):
        if result == StepResult.ERROR:
            # A failed step must stop the profile visibly rather than sit there
            # looking active. StepState.ERROR already existed and was never used
            # anywhere; this is the case it was meant for.
            step_current = self.find_by_id(step.id)
            step_current.status = StepState.ERROR

            async def report():
                await self.save()
                try:
                    self.cbpi.notify(
                        "Mash Profile",
                        "Step '{}' failed and the profile has stopped. Check the "
                        "log, then start to retry it or next to skip it.".format(
                            step.name
                        ),
                        NotificationType.ERROR,
                    )
                except Exception as e:
                    logging.warning("Could not notify about the failed step: %s", e)

            asyncio.create_task(report())
            return

        if result == StepResult.NEXT:
            step_current = self.find_by_id(step.id)
            step_current.status = StepState.DONE

            async def wrapper():
                await self.save()
                # Moving on by itself: a step that waits for the brewer is held.
                await self.start(by_brewer=False)

            asyncio.create_task(wrapper())

    def find_by_status(self, status):
        return next((item for item in self.profile if item.status == status), None)

    def find_by_id(self, id):
        return next((item for item in self.profile if item.id == id), None)

    def get_index_by_id(self, id):
        return next((i for i, item in enumerate(self.profile) if item.id == id), None)

    def push_udpate(self, complete=False):
        if complete is True:
            self.cbpi.ws.send(dict(topic="mash_profile_update", data=self.get_state()))
            for item in self.profile:
                self.cbpi.push_update(
                    topic="cbpi/stepupdate/{}".format(item.id), data=(item.to_dict())
                )
        else:
            self.cbpi.ws.send(
                dict(
                    topic="step_update",
                    data=list(map(lambda item: item.to_dict(), self.profile)),
                )
            )

            step = self.find_by_status(StepState.ACTIVE)
            if step != None:
                self.cbpi.push_update(
                    topic="cbpi/stepupdate/{}".format(step.id), data=(step.to_dict())
                )

    async def start_step(self, step, by_brewer=False):
        try:
            logging.info("Try to start step %s" % step)
            if step.instance is not None:
                step.instance.started_by_brewer = bool(by_brewer)
            await step.instance.start()
            step.status = StepState.ACTIVE
            if self._held_step_id == step.id:
                self._held_step_id = None
        except Exception as e:
            self.cbpi.notify(
                "Error",
                "Can't start step. Please check step in Mash Profile",
                NotificationType.ERROR,
            )
            logging.error("Failed to start step %s" % step)

    async def call_action(self, id, action, parameter) -> None:
        logging.info("Step Controller - call all Action {} {}".format(id, action))
        try:
            item = self.find_by_id(id)
            if item is None:
                logging.error("Step Controller - no such step %s", id)
                return
            method = resolve_action(item.instance, action)
            if method is None:
                logging.error(
                    "Step Controller - refused undeclared action %r on %s - "
                    "only methods decorated with @action can be called",
                    action,
                    id,
                )
                return
            await method(**normalize_action_parameters(parameter))
        except Exception as e:
            logging.error(
                "Step Controller -Failed to call action on {} {} {}".format(
                    id, action, e
                )
            )

    async def load_recipe(self, data):
        try:
            await self.shutdown()
        except:
            pass

        def add_runtime_data(item):
            item["status"] = "I"
            item["id"] = shortuuid.uuid()
            # A recipe is settings. What a step recorded about a run is not:
            # underscored props are runtime bookkeeping (CBPiStep.ELAPSED_PROP,
            # RESUME_UNTIL_PROP), and a recipe saved mid-brew carried them into
            # the next - a rest shortened by the time already run, or a step
            # allowed to carry on by itself after a restart.
            props = item.get("props")
            if isinstance(props, dict):
                for key in [k for k in props if str(k).startswith("_")]:
                    del props[key]

        list(map(lambda item: add_runtime_data(item), data.get("steps")))
        with open(self.path, "w") as file:
            json.dump(data, file, indent=4, sort_keys=True)
        self._held_step_id = None
        self.load()
        self.push_udpate(complete=True)

    async def clear(self):
        try:
            await self.shutdown()
        except:
            pass

        data = dict(basic=dict(), steps=[])
        with open(self.path, "w") as file:
            json.dump(data, file, indent=4, sort_keys=True)

        self._held_step_id = None
        self.load()
        self.push_udpate(complete=True)

    async def savetobook(self):
        name = shortuuid.uuid()
        path = os.path.join(
            self.cbpi.config_folder.get_file_path("recipes"), "{}.yaml".format(name)
        )
        data = dict(
            basic=self.basic_data,
            steps=list(map(lambda item: item.to_dict(), self.profile)),
        )
        with open(path, "w") as file:
            yaml.dump(data, file)
        self.push_udpate()
