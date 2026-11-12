import asyncio
import copy
import inspect
import json
import logging
import math
import os
import os.path
import re
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
from ..api.timer import Timer


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

    MASH_PH_REMINDER_KEY = "_mash_ph_reminder"
    MASH_PH_REMINDER_DELAY = 900

    # When a step first started, in brewing clock seconds: the brew-day screen's
    # graphs tag where each step started, and start DAY at the first. Kept with
    # the step, so a reload or a restart still shows them.
    STARTED_AT_PROP = "_started_at"

    #: What a listener is told (add_listener).
    LISTENER_EVENTS = ("step_started", "step_held", "timer_started", "timer_changed",
                       "timer_stopped", "timer_done", "step_done", "step_failed",
                       "stopped", "reset", "loaded")

    # The listeners (add_listener), and the tasks of async ones still running.
    # Class defaults too, for a controller assembled without __init__.
    _listeners = ()
    _listener_tasks = ()

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
        # Prompts kept for every page until answered or no longer applying
        # (_ask_kept): notification id -> (kind, step id, still_applies).
        self._kept_prompts = {}
        self._mash_ph_reminder_enabled = False
        self._mash_ph_reminder_ready = False
        self._mash_ph_reminder_task = None
        self._mash_ph_prompt_id = None
        self._mash_ph_shutting_down = False
        self.cbpi.app.on_cleanup.append(self.shutdown)

    async def init(self):
        logging.info("INIT STEP Controller")
        self.load(startActive=True)
        self._mash_ph_reminder_ready = True
        self._schedule_mash_ph_reminder()
        if self._resume_after_startup is not None:
            # Not now: the actors and kettles a step switches on are set up
            # after this controller (CraftBeerPi.init_serivces). The app's
            # start-up hooks run once everything is.
            self.cbpi.app.on_startup.append(self._start_resume)

    def enable_mash_ph_reminder(self) -> dict:
        """Opt in at plugin setup/startup, once per server process.

        Only a subsequent, genuine MashIn acknowledgement starts a new timer.
        A saved timer from this run is restored, never inferred from step age.
        Repeated registration is harmless. Returns the same read-only snapshot
        as mash_ph_reminder_status().
        """
        self._mash_ph_reminder_enabled = True
        self._schedule_mash_ph_reminder()
        return self.mash_ph_reminder_status()

    def mash_ph_reminder_status(self) -> dict:
        """A detached snapshot; reading it never arms or acknowledges anything."""
        enabled = getattr(self, "_mash_ph_reminder_enabled", False)
        state = copy.deepcopy(self.basic_data.get(self.MASH_PH_REMINDER_KEY))
        result = dict(enabled=enabled, state="waiting_for_mash_in" if enabled else "disabled")
        if state:
            result.update(
                state=state["status"] if enabled else "disabled",
                run_id=state["run_id"],
                step_id=state["step_id"],
                confirmed_at=state["confirmed_at"],
                due_at=state["due_at"],
                remaining_seconds=max(0.0, state["due_at"] - clock.now()),
            )
        return result

    def _cancel_mash_ph_reminder(self, clear=False):
        task = getattr(self, "_mash_ph_reminder_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._mash_ph_reminder_task = None
        notification_id = getattr(self, "_mash_ph_prompt_id", None)
        if notification_id is not None:
            self.cbpi.notification.retire(notification_id)
        self._mash_ph_prompt_id = None
        # getattr: a controller assembled without __init__, as
        # test_mashin_holds_temp.py builds one, has no basic_data, and reset_all
        # comes here first.
        basic = getattr(self, "basic_data", None)
        if clear and basic is not None:
            basic.pop(self.MASH_PH_REMINDER_KEY, None)

    def _mash_ph_anchor_exists(self, state):
        step = self.find_by_id(state["step_id"])
        return (step is not None and step.type == "MashInStep"
                and step.status in (StepState.ACTIVE, StepState.STOP, StepState.DONE))

    def _load_mash_ph_reminder(self):
        state = self.basic_data.get(self.MASH_PH_REMINDER_KEY)
        if state is None:
            return
        try:
            if (not isinstance(state, dict) or state.get("version") != 1
                    or not isinstance(state.get("run_id"), str) or not state["run_id"]
                    or state.get("status") not in ("pending", "shown", "acknowledged")
                    or not all(isinstance(state.get(key), (int, float))
                               and not isinstance(state[key], bool)
                               and math.isfinite(state[key])
                               for key in ("confirmed_at", "due_at"))
                    or not self._mash_ph_anchor_exists(state)):
                raise ValueError("invalid or no longer belongs to this run")
            saved = state.get("clock")
            if saved is not None:
                if (not isinstance(saved, dict)
                        or not all(isinstance(saved.get(key), (int, float))
                                   and not isinstance(saved[key], bool)
                                   and math.isfinite(saved[key])
                                   for key in ("brewing_at", "real_at", "scale"))
                        or saved["scale"] <= 0):
                    raise ValueError("invalid clock checkpoint")
                # ScaledTimeSource starts at the real epoch in each process.
                # Translate the saved deadline, rather than inheriting the old
                # process's synthetic epoch. Offline time uses the last rate.
                offline = max(0.0, clock.RealTimeSource().now() - saved["real_at"])
                remaining = max(0.0, state["due_at"] - saved["brewing_at"]
                                - offline * saved["scale"])
                state["due_at"] = clock.now() + remaining
                state.pop("clock")
        except (KeyError, TypeError, ValueError) as error:
            logging.warning("Discarding mash pH reminder: %s", error)
            self.basic_data.pop(self.MASH_PH_REMINDER_KEY, None)

    def _checkpoint_mash_ph_reminder(self):
        state = self.basic_data.get(self.MASH_PH_REMINDER_KEY)
        if state is None:
            return
        if not self._mash_ph_anchor_exists(state):
            self._cancel_mash_ph_reminder(clear=True)
            return
        if isinstance(clock.get_source(), clock.ScaledTimeSource):
            state["clock"] = dict(
                brewing_at=clock.now(), real_at=clock.RealTimeSource().now(),
                scale=clock.scale(),
            )
        else:
            state.pop("clock", None)

    async def _mash_in_confirmed(self, instance):
        """Called only by MashIn.next(), while its live instance waits for malt."""
        if not getattr(self, "_mash_ph_reminder_enabled", False):
            return False
        step = self.find_by_id(instance.id)
        if (step is None or step.instance is not instance
                or step.status != StepState.ACTIVE or not instance.running
                or instance.task is None or instance.task.done()
                or not getattr(instance, "_waiting_for_malt", False)
                or not instance.props.get(instance.WAITING_FOR_MALT_PROP, False)):
            logging.info("Not arming mash pH reminder: no live malt acknowledgement")
            return False
        if self.basic_data.get(self.MASH_PH_REMINDER_KEY) is not None:
            return False
        confirmed_at = clock.now()
        self.basic_data[self.MASH_PH_REMINDER_KEY] = dict(
            version=1, run_id=shortuuid.uuid(), step_id=step.id, status="pending",
            confirmed_at=confirmed_at, due_at=confirmed_at + self.MASH_PH_REMINDER_DELAY,
        )
        # The timer before the save. The timer is what reminds the brewer; the
        # save only lets a restart bring it back. Saved first, a failed write
        # left the reminder "pending" with nothing counting down to it.
        self._schedule_mash_ph_reminder()
        await self.save()
        return True

    def _schedule_mash_ph_reminder(self):
        if (not getattr(self, "_mash_ph_reminder_enabled", False)
                or not getattr(self, "_mash_ph_reminder_ready", False)
                or getattr(self, "_mash_ph_shutting_down", False)):
            return
        state = self.basic_data.get(self.MASH_PH_REMINDER_KEY)
        if state is None or state["status"] == "acknowledged":
            return
        task = getattr(self, "_mash_ph_reminder_task", None)
        if ((task is not None and not task.done())
                or getattr(self, "_mash_ph_prompt_id", None) is not None):
            return
        task = asyncio.create_task(self._run_mash_ph_reminder(state["run_id"]))
        self._mash_ph_reminder_task = task
        task.add_done_callback(self._mash_ph_reminder_done)

    @staticmethod
    def _mash_ph_reminder_done(task):
        if not task.cancelled() and task.exception() is not None:
            logging.error("Mash pH reminder failed: %s", task.exception())

    async def _wait_for_mash_ph_due(self, run_id):
        """Until the reminder is due, keeping up with the clock's speed.

        In slices of at most a real second - clock.scale() brewing seconds -
        rather than one sleep, which keeps the speed the clock had when it
        began. A simulator sets SIM_TIME_SCALE from a loop a moment after
        start-up, by when a restored reminder is already waiting, and again
        whenever the setting changes: one sleep begun at 1x still took 15 real
        minutes once the clock ran at 60x, where it should take 15 seconds.
        Step timers wait in slices for the same reason.
        """
        while True:
            state = self.basic_data.get(self.MASH_PH_REMINDER_KEY)
            if state is None or state.get("run_id") != run_id:
                return
            due = state["due_at"]
            if clock.now() >= due:
                return
            await clock.sleep_until(min(clock.now() + clock.scale(), due))

    async def _run_mash_ph_reminder(self, run_id):
        state = self.basic_data[self.MASH_PH_REMINDER_KEY]
        if state["status"] == "pending":
            await self._wait_for_mash_ph_due(run_id)
        state = self.basic_data.get(self.MASH_PH_REMINDER_KEY)
        if (state is None or state["run_id"] != run_id
                or state["status"] == "acknowledged" or not self._mash_ph_anchor_exists(state)):
            return
        # "shown" survives a restart, but an open notification does not. Rebuild
        # that same unacknowledged question, not another countdown.
        state["status"] = "shown"
        await self.save()

        async def acknowledge():
            current = self.basic_data.get(self.MASH_PH_REMINDER_KEY)
            if current is None or current["run_id"] != run_id:
                logging.info("Ignoring an old mash pH acknowledgement")
                return
            current["status"] = "acknowledged"
            notification_id, self._mash_ph_prompt_id = self._mash_ph_prompt_id, None
            if notification_id is not None:
                self.cbpi.notification.retire(notification_id)
            await self.save()

        self._mash_ph_prompt_id = self.cbpi.notify(
            "Check mash pH",
            "It has been 15 minutes since you confirmed mash-in. Check mash pH. "
            "Acknowledging this reminder does not record a measurement or change the brew.",
            NotificationType.INFO,
            action=[NotificationAction("OK", acknowledge)], keep=True,
        )

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
            # Paused when the server stopped - by the brewer, or by an earlier
            # restart whose question was never answered. Scott, 3 Nov: "If we
            # were in the middle of a recipe or current boil step, then an
            # alert should pop-up and ask if I want to resume the interrupted
            # step." It used to come back paused with no question. Asked,
            # never carried on by itself: someone paused it.
            paused = self.find_by_status(StepState.STOP)
            if paused is not None:
                logging.warning(
                    "Step '%s' was paused when the server stopped; asking whether "
                    "to resume it.", paused.name,
                )
                self._ask_about_interrupted(paused, paused=True)
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

    def _ask_about_interrupted(self, interrupted, reason=None, paused=False):
        """The "Resume brew?" question, with why it is asked when that is news.

        `paused` when the step was already paused as the server stopped,
        rather than running."""
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
            self._ask_kept(
                "resume", interrupted, lambda step: step.status == StepState.STOP,
                "Resume brew?",
                "'{}' was {} when the server stopped. {} Check your kettle "
                "first, then choose.".format(
                    interrupted.name, "paused" if paused else "interrupted", detail),
                NotificationType.WARNING,
                action=[
                    NotificationAction("Resume", self.resume),
                    NotificationAction("Skip this step", self.next),
                ],
            )
        except Exception as e:
            logging.warning("Could not ask about the interrupted step: %s", e)

    def _ask_kept(self, kind, step, still_applies, title, message, type, action):
        """Ask the brewer in a prompt kept until it is answered.

        Shown on every page, open now or opened later - the phone as well as
        the laptop - and taken down from all of them when a button is pressed
        on one, or once `still_applies(step)` is no longer true: the brewer
        answered some other way, with Start or Reset (_retire_prompts).

        For the questions asked as the server starts after a restart - "Resume
        brew?", the boil waiting to be started - which reached only the pages
        open at the time, and that is none: a page cannot connect before the
        server is up (DEFECTS M47). One prompt of a kind for a step at a time.
        Returns the notification's id.
        """
        prompts = self.__dict__.setdefault("_kept_prompts", {})
        for earlier, (k, step_id, _) in list(prompts.items()):
            if k == kind and step_id == step.id:
                self._retire_prompt(earlier)
        notification_id = self.cbpi.notify(title, message, type, action=action, keep=True)
        if notification_id is not None:
            prompts[notification_id] = (kind, step.id, still_applies)
        return notification_id

    def keep_step_prompt(self, step_id, kind, notification_id, while_paused=False):
        """Take a step's own prompt down from every page once it no longer applies.

        For the prompts a step sends itself with notify(keep=True): the malt,
        a notification step's question, the wort transfer, the boil's hop, lid
        and first-wort reminders. Kept, they reach a page opened later - a
        phone opened mid-boil, or a page reloaded, showed no hop alert waiting
        (the dashboard agent's review of the brew-day screen, 9 Nov). And they
        go when the step ends, is reset, or the profile is replaced; and when
        it is paused or fails, unless `while_paused`: the boil does not give a
        reminder again when it is started again (DEFECTS M36), so its
        reminders stay until answered. A question whose button acts on the
        step goes with the pause: after ALL OFF a notification step's "Next
        Step" stayed on screen, and pressing it did nothing. One prompt of a
        kind for a step at a time.
        """
        if not notification_id or self.find_by_id(step_id) is None:
            return
        states = ((StepState.ACTIVE, StepState.STOP, StepState.ERROR) if while_paused
                  else (StepState.ACTIVE,))
        prompts = self.__dict__.setdefault("_kept_prompts", {})
        for earlier, (k, other, _) in list(prompts.items()):
            if k == kind and other == step_id and earlier != notification_id:
                self._retire_prompt(earlier)
        prompts[notification_id] = (kind, step_id, lambda step: step.status in states)

    def _retire_prompts(self, everything=False):
        """Take down the kept prompts that no longer apply - or, when the profile
        is reset or replaced, all of them. Run on every save, which follows
        every change of a step's status."""
        prompts = self.__dict__.get("_kept_prompts") or {}
        for notification_id, (kind, step_id, still_applies) in list(prompts.items()):
            if not everything:
                step = self.find_by_id(step_id)
                try:
                    if step is not None and still_applies(step):
                        continue
                except Exception as e:  # noqa: BLE001 - not knowing is not a reason to keep it
                    logging.debug("Could not tell whether a '%s' prompt applies: %s", kind, e)
            self._retire_prompt(notification_id)

    def _retire_prompt(self, notification_id):
        (self.__dict__.get("_kept_prompts") or {}).pop(notification_id, None)
        retire = getattr(getattr(self.cbpi, "notification", None), "retire", None)
        if retire is None:
            return
        try:
            retire(notification_id)
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not take down a prompt that no longer applies: %s", e)

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
            self._ask_kept(
                "carried on", step, lambda _step: True,
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

        self._cancel_mash_ph_reminder()
        self._mash_ph_shutting_down = False
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

        self._load_mash_ph_reminder()
        self._schedule_mash_ph_reminder()

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
                # A warning, not a question: it answers whoever made the
                # change, and the step form shows them why (http_update's
                # 409). With an OK that told CraftBeerPi nothing, the
                # brew-day screen logged it as a question waiting for every
                # screen, and a page reloaded showed that line in amber with
                # nothing to press (the dashboard agent, 12 Nov, late
                # afternoon).
                self.cbpi.notify(
                    existing.name,
                    self.EDIT_WHILE_RUNNING.format(existing.name),
                    NotificationType.WARNING,
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
        self._checkpoint_mash_ph_reminder()
        data = dict(
            basic=self.basic_data,
            steps=list(map(lambda item: item.to_dict(), self.profile)),
        )
        atomic_write_json(self.path, data, sort_keys=True)
        self._retire_prompts()
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
        so do Start and Next. Asked again after a restart (_recover_hold). The
        notification is kept for every page until the step starts (_ask_kept).
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
            self._ask_kept(
                "hold", step,
                lambda s: s.status == StepState.INITIAL and self._held_step_id == s.id,
                step.name, message, NotificationType.WARNING,
                action=[NotificationAction(button, start_held)],
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not ask the brewer to start '%s': %s", step.name, e)
        # The step may have asked for something in its question - BoilStep the
        # first-wort hops - and must not ask again as it starts.
        held = getattr(step.instance, "held_for_brewer", None)
        if callable(held):
            try:
                held()
            except Exception as e:  # noqa: BLE001
                logging.warning("Step '%s' could not note its hold: %s", step.name, e)
        self._tell("step_held", step)

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
        # Kept, as the steps' own questions are, so a page opened after the
        # brew - a phone, or the monitor reloaded - is shown it too: the
        # brew-day screen's line for it said "waiting for you", and its title
        # bar "No alarms", with nothing to answer it with (DEFECTS M81). Until
        # OK, Reset or another recipe.
        last = self.profile[-1] if self.profile else None
        if last is not None:
            self._ask_kept(
                "complete", last, lambda step: step.status == StepState.DONE,
                "Brewing Complete",
                "Now the yeast will take over",
                NotificationType.INFO,
                action=[NotificationAction("OK")],
            )
        else:
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
        if self.basic_data.get(self.MASH_PH_REMINDER_KEY) is not None:
            self._cancel_mash_ph_reminder(clear=True)
            await self.save()

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
                self._tell("step_done", step)
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
                self._tell("stopped", step)

                await self.save()
            except Exception as e:
                logging.error("Failed to stop step - Id: %s" % step.id)

    async def reset_all(self):
        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Please stop before reset")
            return

        self._cancel_mash_ph_reminder(clear=True)
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
            # convention: the mash-in waiting for its malt, the boil reminders
            # already given (DEFECTS M35, M36). Left behind, the next brew's
            # mash-in would ask for malt in cold water. The keys cleared above
            # are left to their own code and its fallbacks.
            cleared_above = (CBPiStep.ELAPSED_PROP, CBPiStep.RESUME_UNTIL_PROP,
                             CBPiStep.RESUME_BY_PROP)
            try:
                props = item.props.to_dict() if hasattr(item.props, "to_dict") \
                    else dict(item.props or {})
                for key in [k for k in props
                            if str(k).startswith("_") and k not in cleared_above]:
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
        # Starting over: no question about the brew before still stands.
        self._retire_prompts(everything=True)
        self._tell("reset")
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
        self._mash_ph_shutting_down = True
        self._cancel_mash_ph_reminder()
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
            self._tell("step_failed", step_current)

            async def report():
                await self.save()
                try:
                    self.cbpi.notify(
                        "Brew Steps",
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
            self._tell("step_done", step_current)

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

    def add_listener(self, fn):
        """Tell `fn` about the brew steps as they change: fn(event, snapshot).

        For a plugin that follows the brew without running it. Porter, for
        BrewScribe, 5 Nov: "all of brewScribe's reminders should appear from
        craftbeerpi: Check mash pH again before mash-out or transfer" - which
        needs to know when a rest will end, and polling once a real second
        cannot tell that on a simulator running 60 times faster.

        `event` is one of LISTENER_EVENTS, and `snapshot` is snapshot(event),
        a copy of its own. A plain function is called on the event loop right
        after the change, so it must return quickly; an `async def` is run as a
        task of its own, just after. Read-only by contract: a listener must not
        start, stop, move on or reset the brew steps, nor switch anything.
        Whatever it raises is logged, and the brew carries on, as do the other
        listeners. Adding the same one twice keeps one. Not saved: a plugin
        adds its listener again on every start.
        """
        if not callable(fn):
            raise TypeError("a listener must be callable, not %r" % (fn,))
        if not isinstance(self._listeners, list):
            self._listeners = []
        if fn not in self._listeners:
            self._listeners.append(fn)
        Timer.add_observer(self._timer_told)

    def remove_listener(self, fn):
        """Stop telling `fn`. One that was never added: nothing happens."""
        if isinstance(self._listeners, list) and fn in self._listeners:
            self._listeners.remove(fn)
        if not self._listeners:
            Timer.remove_observer(self._timer_told)

    def snapshot(self, event=None, step=None):
        """The brew steps as plain data, for a listener - a copy: changing it
        changes nothing here.

        event: what happened (LISTENER_EVENTS); None when asked for directly.
        at: when, in brewing-clock seconds (cbpi.api.clock.now()). step_id: the
        step it happened to, None for reset and loaded. active_id: the step
        running. held_id: the step the brew waits before for the brewer (the
        boil). basic: the recipe's basic data, without the mash pH reminder's
        bookkeeping - mash_ph_reminder_status() reads that. steps, in order:
        id, name, type, status ("I" waiting, "A" active, "S" stopped, "D" done,
        "E" failed), props, state_text, state_text2, and timer: None, or
        Timer.state(), in brewing-clock seconds.
        """
        basic = copy.deepcopy(self.basic_data) if isinstance(self.basic_data, dict) else {}
        basic.pop(self.MASH_PH_REMINDER_KEY, None)
        profile = getattr(self, "profile", None) or []
        active = self.find_by_status(StepState.ACTIVE) if profile else None
        steps = []
        for item in profile:
            entry = copy.deepcopy(item.to_dict())
            timer = getattr(item.instance, "timer", None)
            entry["timer"] = timer.state() if isinstance(timer, Timer) else None
            steps.append(entry)
        return {
            "event": event,
            "at": clock.now(),
            "step_id": getattr(step, "id", None),
            "active_id": active.id if active is not None else None,
            "held_id": self._held_step_id,
            "basic": basic,
            "steps": steps,
        }

    def _tell(self, event, step=None):
        """Tell the listeners (add_listener) - never raising, never waiting."""
        listeners = list(self._listeners or ())
        if not listeners:
            return
        try:
            snap = self.snapshot(event, step)
        except Exception:  # noqa: BLE001 - never the reason a brew stops
            logging.exception("Could not tell the step listeners about %s", event)
            return
        for fn in listeners:
            try:
                result = fn(event, copy.deepcopy(snap))
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    if not isinstance(self._listener_tasks, set):
                        self._listener_tasks = set()
                    self._listener_tasks.add(task)
                    task.add_done_callback(self._listener_finished)
            except Exception:  # noqa: BLE001
                logging.exception("A step listener failed on %s", event)

    def _listener_finished(self, task):
        if isinstance(self._listener_tasks, set):
            self._listener_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logging.error("A step listener failed: %r", task.exception(),
                          exc_info=task.exception())

    def _timer_told(self, timer, what):
        """Timer.add_observer: if `timer` is one of these steps', tell the listeners."""
        step = next((item for item in getattr(self, "profile", None) or []
                     if getattr(item.instance, "timer", None) is timer), None)
        if step is not None:
            self._tell(what, step)

    #: A step that runs the wort off to the boil kettle, by its name: the names
    #: BrewScribe reads (its recipes.step_phase) and the BeerSmith guide asks
    #: for - Sparge, Fly Sparge, Batch Sparge, Continuous Sparge, Lauter,
    #: Runoff. Not one that only gets ready for it: "Heat sparge water".
    _RUNOFF_NAME = re.compile(
        r"^(?:step \d+ )?(?:(?:start|begin) )?(?:(?:fly|batch|continuous) )?"
        r"(?:sparge|sparging|lauter|lautering|run ?off)(?: |$)")
    _NOT_RUNOFF = re.compile(
        r"\b(?:water|heat|heating|prepare|preparation|salts|treatment)\b")
    #: A step that ends the mash, by its name: the names BrewScribe reads for it
    #: and the BeerSmith guide asks for - Mash Out, Mashout, Mash-Out, End of
    #: Mash - and MMuM's MashOut.
    _MASH_OUT_NAME = re.compile(r"^(?:step \d+ )?(?:mash ?out|end of mash)(?: |$)")

    @staticmethod
    def _plain_name(step):
        """A step's name in lower case, its words split: "MashOut" -> "mash out"."""
        name = re.sub(r"([a-z])([A-Z])", r"\1 \2", str(getattr(step, "name", "") or ""))
        return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()

    @classmethod
    def _collects_wort(cls, step):
        """Is `step` the runoff to the boil kettle, by its type or its name?"""
        if getattr(step, "type", None) == "HeatStep":
            return False
        # A SpargeStep is the runoff whatever it is called (Scott, 10 Nov).
        if getattr(step, "type", None) == "SpargeStep":
            return True
        name = cls._plain_name(step)
        return bool(cls._RUNOFF_NAME.match(name)) and not cls._NOT_RUNOFF.search(name)

    @classmethod
    def _ends_mash(cls, step):
        """Is `step` a mash out, by its name?"""
        return bool(cls._MASH_OUT_NAME.match(cls._plain_name(step)))

    def _first_wort_at_runoff(self, step):
        """Ask for the boil's first-wort hops as the runoff to the boil kettle starts.

        Scott, 5 Nov: first-wort hops "should be correctly supported". The boil
        asked for them as it started, and since the profile holds before the
        boil (4.7.7.post23) that is after the wort is in the kettle. A step
        named for the runoff starts as the first runnings head for the kettle,
        so the first boil after it asks then, once a brew. With no such step,
        the hold before the boil asks (BoilStep.wait_for_brewer); with no hold
        (CONFIRM_BEFORE_BOIL No), the boil's start, which is the end of the mash.
        """
        try:
            if not self._collects_wort(step):
                return
            index = self.get_index_by_id(step.id)
            for later in self.profile[index + 1:]:
                due = getattr(later.instance, "first_wort_due", None)
                if not callable(due):
                    continue
                hops = due()
                if hops is None:
                    return
                later.instance.first_wort_asked()
                # Kept for a page opened later, until answered or the boil is
                # over: it is not asked again this brew.
                self._ask_kept(
                    "first-wort", later, lambda boil: boil.status != StepState.DONE,
                    "First Wort Hop Addition!",
                    "Please add {} to the boil kettle before the first runnings "
                    "reach it".format(hops or "the first-wort hops"),
                    NotificationType.WARNING,
                    action=[NotificationAction("Added")],
                )
                return
        except Exception as e:  # noqa: BLE001 - a reminder never stops the brew
            logging.warning("Could not ask for the first-wort hops at '%s': %s",
                            getattr(step, "name", "?"), e)

    async def start_step(self, step, by_brewer=False):
        try:
            logging.info("Try to start step %s" % step)
            if step.instance is not None:
                step.instance.started_by_brewer = bool(by_brewer)
            await step.instance.start()
            step.status = StepState.ACTIVE
            # Only the first start: a stop and a resume, or a restart, is the
            # same step going on. Underscored, so an edit keeps it and starting
            # over or loading another recipe clears it (update, reset_all,
            # load_recipe).
            try:
                if step.props is not None and not step.props.get(self.STARTED_AT_PROP, None):
                    step.props[self.STARTED_AT_PROP] = round(clock.now(), 3)
            except Exception as e:  # noqa: BLE001 - a graph's tag never stops a step
                logging.warning("Could not note when '%s' started: %s", step.name, e)
            if self._held_step_id == step.id:
                self._held_step_id = None
            self._tell("step_started", step)
            self._first_wort_at_runoff(step)
        except Exception as e:
            self.cbpi.notify(
                "Error",
                "Can't start the step. Check it on the Brew Steps page.",
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
        data.get("basic", {}).pop(self.MASH_PH_REMINDER_KEY, None)
        # In one write: an import or the recipe book replaces the whole profile,
        # and a write broken part-way left an unreadable file, which load()
        # then set aside for an empty profile (DEFECTS M39).
        atomic_write_json(self.path, data, indent=4, sort_keys=True)
        self._held_step_id = None
        self.load()
        self._retire_prompts(everything=True)
        self.push_udpate(complete=True)
        self._tell("loaded")

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
        self._retire_prompts(everything=True)
        self.push_udpate(complete=True)
        self._tell("loaded")

    #: Where an import came from, in a recipe the book keeps for it - as
    #: RecipeController.IMPORTED_FROM. Not part of a recipe the brewer saves.
    IMPORTED_FROM_KEY = "imported_from"

    def book_data(self):
        """The brew steps as the recipe book keeps them: the recipe, not this run.

        What Save writes, and what an import keeps in the book. Not the mash pH
        reminder, which belongs to this brew; and not where an import came
        from: a copy the brewer saves is the brewer's own, and an import of the
        same name never replaces it (RecipeController.keep_import).
        """
        basic = copy.deepcopy(self.basic_data)
        basic.pop(self.MASH_PH_REMINDER_KEY, None)
        basic.pop(self.IMPORTED_FROM_KEY, None)
        return dict(
            basic=basic,
            steps=list(map(lambda item: item.to_dict(), self.profile)),
        )

    async def savetobook(self):
        name = shortuuid.uuid()
        path = os.path.join(
            self.cbpi.config_folder.get_file_path("recipes"), "{}.yaml".format(name)
        )
        data = self.book_data()
        with open(path, "w") as file:
            yaml.dump(data, file)
        self.push_udpate()
