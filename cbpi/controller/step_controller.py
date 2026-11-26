import asyncio
import copy
import inspect
import json
import logging
import math
import os
import os.path
import re
import time
from datetime import datetime

from cbpi.api.persist import atomic_write_json, atomic_write_text, quarantine
from cbpi.api.system_clock import ask_timedatectl
from os import listdir
from os.path import isfile, join

import cbpi
import shortuuid
import yaml
from cbpi.api import clock, presence, timefmt
from cbpi.api.dataclasses import (NotificationAction, NotificationType, Props,
                                  Step)
from tabulate import tabulate

from ..api.decorator import normalize_action_parameters, resolve_action
from ..api.step import CBPiStep, StepMove, StepResult, StepState
from ..api.timer import Timer
from .pump_starts import PumpStarts


def system_clock_synchronized():
    """Whether the system clock has been set from a time server since boot,
    by the rule the brew-day timers use too (cbpi.api.system_clock).

    True when timedatectl says so. False where timedatectl exists and doesn't:
    it says no, fails, doesn't answer in time or answers something else - a
    step that may heat must not decide by a clock nobody could confirm. None
    where there is no timedatectl at all (Windows), and there a PC's
    battery-backed clock keeps time through a power cut anyway.

    A Raspberry Pi has no battery-backed clock. After a power cut it boots
    with the time it last saved (fake-hwclock), which can be hours behind,
    and the right time comes from the network a minute or so later. Anything
    that decides by the time of day right after a boot must wait for it.
    """
    return ask_timedatectl()[0]


class StartRefused(Exception):
    """START, told which recipe it was pressed on, started nothing (StepController.start).

    reason: "recipe_changed" - the loaded profile isn't that recipe any more -
    or "already_running" - it is, and a step of it runs already. name and
    steps say what is loaded: the recipe's name (None when nothing is loaded,
    or it has no name) and its steps' ids, in order.
    """

    def __init__(self, reason, name, steps):
        super().__init__(reason)
        self.reason = reason
        self.name = name
        self.steps = steps


class StepRunning(Exception):
    """A load or a clear refused while a step runs, with nothing changed (StepController.load_recipe, clear).

    step: the running step's name. detail: the sentence for the brewer, which says what to do. Either would stop the
    running step first, which ends a mash rest and switches its kettle's logic off, then replace or empty the brew
    steps.
    """

    def __init__(self, step, detail):
        super().__init__(detail)
        self.step = step
        self.detail = detail


class OrderRefused(Exception):
    """A new order of the brew steps, or a step's place, refused with nothing changed (StepController.reorder, add).

    status: the HTTP status for it. error: bad_body, steps_changed or begun. more: the rest of the answer's body.
    """

    def __init__(self, status, error, **more):
        super().__init__(error)
        self.status = status
        self.error = error
        self.more = more

    def body(self):
        return dict(error=self.error, **self.more)


class BookRefused(OrderRefused):
    """SAVE TO BOOK refused with nothing changed (StepController.savetobook).

    error: bad_body, or not_in_book for a book recipe to replace that isn't there.
    """


class ActionRefused(Exception):
    """A step's declared action said no, with its own reason (StepController.run_action).

    A step refuses by raising ValueError with the reason in its words, in the brewer's language. reason: those
    words. POST /step2/action/{id} answers them, from 4.7.7.post61.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class StepController(PumpStarts):

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
    _wort_prompt = None

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
        # Steps that held the water and asked to move on by themselves this brew: step id -> how many times
        # (_said_self_next). And the "Wort pump held" prompt while it is up (_tell_wort_held).
        self._self_next_refused = {}
        self._wort_prompt = None
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
        UI: next() would raise on task.cancel() against None and return HTTP 500,
        start() refuses with "Steps already running", and reset_all() declines to
        reset while a step is ACTIVE. The lookup is by StepState.ACTIVE, not the
        raw string "A": StepState is a plain Enum, so StepState.ACTIVE == "A" is
        False.

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
        time it was allowed until has not passed, so that BrewStart, after an
        overnight power cut, still has the hot water ready. BrewStart allows it for
        itself and for the step it hands over to, until two hours after the
        time it was asked for; nothing else does. Such a step is still demoted
        here, and picked up again once start-up has finished, the clock has been
        confirmed and the probes have read (_resume_interrupted).
        """
        interrupted = self.find_by_status(StepState.ACTIVE)
        if interrupted is None:
            # Paused when the server stopped - by the brewer, or by an earlier
            # restart whose question was never answered. The brewer is asked
            # whether to resume it, rather than finding it paused with no
            # question; never carried on by itself: someone paused it.
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
        brew?", the boil waiting to be started - which would otherwise reach
        only the pages open at the time, and that is none: a page cannot
        connect before the server is up. One prompt of a kind for a step at a time.
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
        phone opened mid-boil, or a page reloaded, still shows the hop alert
        waiting. And they go when the step ends, is reset, or the profile is
        replaced; and when it is paused or fails, unless `while_paused`: the
        boil does not give a reminder again when it is started again, so its
        reminders stay until answered. A question whose button acts on the
        step goes with the pause: after ALL OFF a notification step's "Next
        Step" would otherwise stay on screen, and pressing it would do nothing.
        One prompt of a kind for a step at a time.
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

    def withdraw_step_prompt(self, step_id, kind):
        """Take a step's kept prompt of this kind down from every page now: the
        question no longer stands. The strike water's temperature changed after
        its step had asked for the water to be moved, and the step asks again
        at the new one (MashInStep.change_target)."""
        prompts = self.__dict__.get("_kept_prompts") or {}
        for notification_id, (k, other, _) in list(prompts.items()):
            if k == kind and other == step_id:
                self._retire_prompt(notification_id)

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

    def _clock_time(self, seconds):
        """'07:00' - or 'Sat 07:00' on another day - on the brewing clock, as
        CLOCK_FORMAT shows times of day: '7:00 AM', 'Sat 7:00 AM' at 12-hour."""
        try:
            moment = datetime.fromtimestamp(float(seconds))
            today = datetime.fromtimestamp(clock.now()).date()
        except (TypeError, ValueError, OverflowError, OSError):
            return "?"
        cbpi = getattr(self, "cbpi", None)
        if moment.date() == today:
            return timefmt.time_of_day(moment, cbpi)
        return timefmt.day_and_time(moment, cbpi)

    async def allow_resume(self, step_id, until, by=None):
        """Let a step carry on by itself if a restart interrupts it before `until`.

        `until` is in seconds on the brewing clock (cbpi.api.clock.now()), `by`
        the name of the step that allows it, said to the brewer when it
        happens. For a step that has promised something by a time - BrewStart's
        hot water by 07:00 - and for the step it hands that promise to.
        Everything else that was running when the server stopped waits for the
        brewer. A reset forgets it.

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

    #: Brewing-clock times a step keeps in its props, with the places they are
    #: rounded to: moved with the clock (clock_moved).
    CLOCK_PROPS = ((CBPiStep.RESUME_UNTIL_PROP, 1), (STARTED_AT_PROP, 3))

    @staticmethod
    def _moved(value, seconds, places):
        """`value` + `seconds`, rounded, if `value` is a finite number; else None."""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        return round(value + seconds, places)

    def clock_moved(self, seconds):
        """The brewing clock's now() has just jumped by `seconds` (CraftBeerPi.
        brewing_clock: a simulator's speed changed, and the clock went back to
        the computer's time). Moves every time the brew steps keep on it by as
        much, so the brew carries on as it was, with as long to go:

        - each step's props: until when it may carry on by itself after a
          restart (_resume_until), and when it first started (_started_at);
        - each step's own times (CBPiStep.clock_moved); its timer moves itself;
        - the basic data's loaded_at, and the mash pH reminder's confirmed_at
          and due_at, so the reminder comes as many minutes after mash-in.

        BrewStart's planned moment is not moved: it plans again
        (replan_after_clock_reset). Saved by the caller. Never raises."""
        for step in list(getattr(self, "profile", None) or []):
            props = step.props
            try:
                for key, places in self.CLOCK_PROPS:
                    moved = self._moved(props.get(key, None), seconds, places) if props is not None else None
                    if moved is not None:
                        props[key] = moved
            except Exception as e:  # noqa: BLE001
                logging.warning("Could not move the clock times of '%s': %s", step.name, e)
            moved = getattr(step.instance, "clock_moved", None)
            if callable(moved):
                try:
                    moved(seconds)
                except Exception as e:  # noqa: BLE001
                    logging.warning("'%s' could not move its times with the clock: %s", step.name, e)
        basic = getattr(self, "basic_data", None)
        if not isinstance(basic, dict):
            return
        loaded = self._moved(basic.get(self.LOADED_AT_KEY, None), seconds, 3)
        if loaded is not None:
            basic[self.LOADED_AT_KEY] = loaded
        reminder = basic.get(self.MASH_PH_REMINDER_KEY)
        if isinstance(reminder, dict):
            for key in ("confirmed_at", "due_at"):
                moved = self._moved(reminder.get(key, None), seconds, 3)
                if moved is not None:
                    reminder[key] = moved

    async def replan_after_clock_reset(self):
        """BrewStart plans again from its settings, as START does: Today and
        Tomorrow are counted from the clock as it is now. After clock_moved.

        Its planned moment (_brewstart_planned) is on the clock as it was, and
        a BrewStart started again would wait for it: Tuesday's 23:15, after an
        accelerated run had taken the clock to Sunday's, for "Today 23:15". So
        each BrewStart not yet done forgets it (its reset()), and one waiting
        now is stopped and started again around that, quietly: the same step
        going on, so nobody is told it started. One that moves its own times
        with the clock (its own clock_moved) is left to it. True if any
        planned again."""
        replanned = False
        for step in list(getattr(self, "profile", None) or []):
            instance = step.instance
            if step.type != self.BREWSTART_TYPE or instance is None or step.status == StepState.DONE:
                continue
            if getattr(type(instance), "clock_moved", CBPiStep.clock_moved) is not CBPiStep.clock_moved:
                continue
            task = getattr(instance, "task", None)
            waiting = step.status == StepState.ACTIVE and task is not None and not task.done()
            try:
                if waiting:
                    await instance.stop()
                await instance.reset()
                if waiting:
                    await instance.start()
                replanned = True
                logging.info("'%s' plans again from its settings, the clock having been reset", step.name)
            except Exception as e:  # noqa: BLE001
                logging.warning("'%s' could not plan again after the clock was reset: %s", step.name, e)
        return replanned

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
        """True once timedatectl says the system clock is synchronized, None
        where there is no timedatectl at all, False if it hasn't said so within
        `timeout` real seconds: it said no, failed, didn't answer in time or
        answered something else (system_clock_synchronized)."""
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

    async def add(self, item: Step, index=None):
        """Add a step, last, or at `index` (0 is first), and return it as saved.

        Raises OrderRefused, adding nothing, for an index that isn't a whole number from 0 to the number of steps
        (400 bad_body), or one at or above a step that has begun (409 begun): the steps still to come run in order,
        after the ones that have begun.
        """
        if index is not None:
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= len(self.profile):
                raise OrderRefused(400, "bad_body", detail="index is a whole number from 0, first, to %d, last."
                                   % len(self.profile))
            self._refuse_above_begun(index)
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
        if index is None:
            self.profile.append(item)
        else:
            self.profile.insert(index, item)
        await self.save()
        await self._props_saved(item)
        return item

    # Told to whoever asks for a new order, or a step's place, above a step that has begun.
    BEGUN = "{} has begun, so it can't move, and no step can go above it."

    def _last_begun(self):
        """(index, step) of the last step that has begun - done, running, stopped or failed - or (-1, None)."""
        for i in range(len(self.profile) - 1, -1, -1):
            if self.profile[i].status != StepState.INITIAL:
                return i, self.profile[i]
        return -1, None

    def _refuse_above_begun(self, index):
        """Raise OrderRefused (409 begun) when `index` is at or above the last step that has begun."""
        last, step = self._last_begun()
        if index <= last:
            name = step.name or "A step"
            raise OrderRefused(409, "begun", detail=self.BEGUN.format(name), step=name)

    async def reorder(self, ids):
        """Put the brew steps in the order of `ids`: every loaded step's id, each once. Saved, and sent to every
        page, once. True when the order changed; the same order changes nothing and saves nothing.

        Raises OrderRefused, changing nothing: 400 bad_body for ids that aren't a list of strings; 409
        steps_changed, with the loaded ids, for ids that aren't exactly the loaded steps' - one missing, extra or
        repeated - as from a page that hasn't seen a step added or deleted; 409 begun when a step that has begun
        would move, or a step would go above one. The steps still to come can be ordered while a step runs, as
        their settings can be changed.
        """
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise OrderRefused(400, "bad_body", detail='Send {"ids": [...]}: every step\'s id, in the new order.')
        loaded = [step.id for step in self.profile]
        if len(ids) != len(set(ids)) or sorted(ids) != sorted(loaded):
            raise OrderRefused(409, "steps_changed", steps=loaded)
        last, _ = self._last_begun()
        if ids[:last + 1] != loaded[:last + 1]:
            self._refuse_above_begun(last)
        if ids == loaded:
            return False
        by_id = {step.id: step for step in self.profile}
        self.profile = [by_id[i] for i in ids]
        await self.save()
        return True

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

        Not while it runs. Building a new instance and swapping it in without
        stopping the running one would leave that one going outside the
        profile: Stop would find nothing active, an import could not reach it,
        and its kettle logic - the HLT element and the pump - would keep going.
        """

        logging.info("update step")
        existing = self.find_by_id(item.id)
        if existing is not None and existing.status == StepState.ACTIVE:
            logging.warning("Refused to change step '%s' while it runs", existing.name)
            try:
                # A warning, not a question: it answers whoever made the
                # change, and the step form shows them why (http_update's
                # 409). With an OK that told CraftBeerPi nothing, the
                # brew-day screen would log it as a question waiting for every
                # screen, and a page reloaded would show that line in amber with
                # nothing to press.
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
            # taken, it would give a reminder again or run time again. So the
            # run's own values always win; the form's are dropped.
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

        The boil needs the brewer at the dashboard first, to run the wort pump
        that moves the wort, rather than a dialog over the whole interface
        asking "Is the wort in the boil kettle?" with the pump behind it.

        So the profile stops before the step. Nothing is active, so nothing
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

    # Holding the strike water for the brewer.
    #
    # BrewStart heats the strike water and holds it; so does a MashIn on the HLT. Nothing after either may start
    # until the brewer is there: he moves the water to the mash tun, maybe doughs in, connects the hoses and pumps,
    # and then presses NEXT, or answers the step's question. So such a step moves on only on his press
    # (cbpi.api.presence), never by asking for it itself - a plugin's mistake, a setting, a timer. And the mash
    # tun's wort pump, with the heat its AUTO sends through the coil, waits until he has said the water is in the
    # mash tun for this brew (strike_move_confirmed). The HLT pump is not held: it may stir the HLT while the strike
    # water heats (cbpi/extension/hltpump).

    #: Said, as a warning titled with the step's name, when a step that holds the water asks to move on by itself.
    SELF_NEXT_TEXT = "{} tried to move on by itself; it's holding the water. Press NEXT when you're ready."
    #: Said when such a step asks again, having been started again to hold the water: it is stopped instead.
    SELF_NEXT_STOPPED_TEXT = ("{} tried again to move on by itself, so it has stopped: nothing heats or moves on "
                              "until you press START to hold the water again, or NEXT to go on.")

    #: In the brew steps' basic data once the brewer has said the strike water is in the mash tun, for this brew:
    #: {"at": when, on the brewing clock; "by": how - "next" (NEXT, or his answer, out of the last step holding the
    #: water, or out of a mash tun step), "start" (he started a mash tun step himself), "pump" (he switched the wort
    #: pump on himself), "auto" (he switched the mash tun's AUTO on himself) or "button" (Water's in, below)}. A
    #: reset, a load or an unload drops it; the Recipe Book never keeps it (BREW_KEYS).
    STRIKE_MOVED_KEY = "strike_moved"

    WORT_HELD_TITLE = "Wort pump held"
    WORT_HELD_TEXT = ("{} wanted to start the wort pump, but nobody has said the strike water is in the mash tun "
                      "yet, so the wort pump stays off and nothing heats the mash through the coil. When the water "
                      "is in the mash tun and the hoses are on, press Water's in.")
    WATER_IN = "Water's in"

    def _kettle_roles(self):
        """{"hlt", "mt", "bk"} as the brew-day screen finds them (cbpi.extension.hltpump.kettle_roles); all None if
        they can't be found."""
        try:
            from cbpi.extension.hltpump import kettle_roles
            return kettle_roles(self.cbpi)
        except Exception as e:  # noqa: BLE001
            logging.debug("Could not find the HLT and the mash tun: %s", e)
            return {"hlt": None, "mt": None, "bk": None}

    @staticmethod
    def _kettle_of(step):
        props = getattr(step, "props", None)
        try:
            return props.get("Kettle", None) if props is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _on_mash_tun(self, step):
        """Whether `step` works on the mash tun (its Kettle)."""
        mt = self._kettle_roles().get("mt")
        return mt is not None and self._kettle_of(step) == mt.id

    def holds_water_for_brewer(self, step):
        """Whether `step` - a brew step, or its running instance - holds the water for the brewer: a BrewStart, a
        MashInStep on a vessel other than the mash tun (the HLT's strike water), or a step that says so itself with
        holds_water_for_brewer() True. Never raises."""
        entry = self.find_by_id(getattr(step, "id", None)) if getattr(step, "id", None) is not None else None
        if entry is None:
            return False
        if entry.type == self.BREWSTART_TYPE:
            return True
        try:
            said = getattr(entry.instance, "holds_water_for_brewer", None)
            if (said() if callable(said) else said) is True:
                return True
        except Exception as e:  # noqa: BLE001
            logging.debug("'%s' could not say whether it holds the water: %s", entry.name, e)
        if entry.type == "MashInStep":
            mt = self._kettle_roles().get("mt")
            kettle = self._kettle_of(entry)
            return mt is not None and bool(kettle) and kettle != mt.id
        return False

    def refuses_self_next(self, step):
        """True, having said so, when `step` holds the water for the brewer (holds_water_for_brewer) and asks to
        move on by itself rather than on his press: CBPiStep.next then carries on holding. Not when the step that
        would start next holds the water too, off the mash tun (_hands_to_holder). Never raises."""
        try:
            if presence.pressed() is not None or not self.holds_water_for_brewer(step):
                return False
            entry = self.find_by_id(step.id)
            if self._hands_to_holder(entry):
                return False
            self._said_self_next(entry)
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not check whether a step may move on by itself: %s", e)
            return False

    def _hands_to_holder(self, entry):
        """Whether the step that starts when `entry` ends - a stopped one first, else the first not begun, as start()
        picks it - holds the water too, on a vessel other than the mash tun: a BrewStart handing over at its time to
        the MashIn on the HLT right after it (the rig page's step 9b), which heats the same water, holds it and asks.
        Moving on so keeps the brew at the water, so it may happen by itself; it doesn't say the water is in."""
        profile = getattr(self, "profile", None) or []
        nxt = next((s for s in profile if s is not entry and s.status == StepState.STOP), None)
        if nxt is None:
            nxt = next((s for s in profile if s is not entry and s.status == StepState.INITIAL), None)
        if nxt is None or self._on_mash_tun(nxt) or not self.holds_water_for_brewer(nxt):
            return False
        logging.info("'%s' hands over by itself to '%s', which holds the water too", entry.name, nxt.name)
        return True

    def _said_self_next(self, entry):
        """The warning, once a brew for each step: logged, and kept for every page until the step ends."""
        refused = self.__dict__.setdefault("_self_next_refused", {})
        refused[entry.id] = refused.get(entry.id, 0) + 1
        if refused[entry.id] > 1:
            logging.debug("'%s' asked again to move on by itself: refused", entry.name)
            return
        logging.warning("'%s' tried to move on by itself, which only the brewer's NEXT may do: it carries on "
                        "holding the water", entry.name)
        try:
            self._ask_kept("self-next", entry, lambda s: s.status == StepState.ACTIVE, entry.name or "Brew Steps",
                           self.SELF_NEXT_TEXT.format(entry.name or "The step"), NotificationType.WARNING,
                           action=[NotificationAction("OK")])
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not tell the brewer that '%s' tried to move on: %s", entry.name, e)

    def _refuse_finished_hold(self, entry):
        """A step holding the water ended with NEXT that wasn't the brewer's press - it went round CBPiStep.next.
        It is started again to hold the water, once; ended so again, it is stopped, which heats nothing."""
        self._said_self_next(entry)
        again = self.__dict__.get("_self_next_refused", {}).get(entry.id, 0) > 1

        async def hold_again():
            if again:
                entry.status = StepState.STOP
                self._tell("stopped", entry)
                logging.warning("'%s' tried again to move on by itself: stopped", entry.name)
                try:
                    self.cbpi.notify(entry.name or "Brew Steps",
                                     self.SELF_NEXT_STOPPED_TEXT.format(entry.name or "The step"),
                                     NotificationType.WARNING)
                except Exception as e:  # noqa: BLE001
                    logging.warning("Could not say that '%s' stopped: %s", entry.name, e)
            else:
                await self.start_step(entry)
            await self.save()

        asyncio.create_task(hold_again())

    def strike_move_confirmed(self):
        """Whether the brewer has said the strike water is in the mash tun, for this brew (STRIKE_MOVED_KEY)."""
        basic = getattr(self, "basic_data", None)
        return isinstance(basic, dict) and isinstance(basic.get(self.STRIKE_MOVED_KEY), dict)

    def _strike_steps_left(self, but=None):
        """The steps holding the water before the mash tun's first step (all of them, with none on the mash tun),
        not yet done, `but` that one."""
        mt = self._kettle_roles().get("mt")
        left = []
        for item in getattr(self, "profile", None) or []:
            if mt is not None and self._kettle_of(item) == mt.id:
                break
            if item is not but and item.status != StepState.DONE and self.holds_water_for_brewer(item):
                left.append(item)
        return left

    def _confirm_strike_moved(self, by):
        """Note that the brewer has said the strike water is in the mash tun (STRIKE_MOVED_KEY). True if that is
        news. Saved by the caller."""
        if self.strike_move_confirmed():
            return False
        if not isinstance(getattr(self, "basic_data", None), dict):
            self.basic_data = {}
        self.basic_data[self.STRIKE_MOVED_KEY] = dict(at=round(clock.now(), 3), by=str(by))
        logging.info("The strike water is in the mash tun, the brewer says (%s): the wort pump may run", by)
        self._retire_wort_prompt()
        return True

    async def confirm_strike_moved(self, by):
        """The brewer has said the strike water is in the mash tun: the wort pump may run (strike_move_refuses)."""
        if self._confirm_strike_moved(by):
            await self.save()
            self.push_udpate(complete=True)

    def _finished_by_press(self, entry):
        """`entry`, holding the water, has been moved on by the brewer's press: if it was the last before the mash
        tun, the strike water is in the mash tun."""
        if not self._strike_steps_left(but=entry):
            self._confirm_strike_moved("next")

    async def strike_move_refuses(self, actor_id, logic_kettle=None):
        """True, having told the brewer, when `actor_id` must stay off because the strike water isn't in the mash
        tun yet, for this brew: the mash tun's wort pump, switched on by anything but his own press of it
        (ActorController.on); and, for the mash tun's AUTO (`logic_kettle` its kettle's id), its wort pump and its
        heater - on a HERMS the HLT element, heating the mash through the coil (CBPiKettleLogic.actor_on). His press
        of the wort pump says the water is in. Never raises."""
        try:
            mt = self._kettle_roles().get("mt")
            if mt is None or not actor_id:
                return False
            if logic_kettle is not None:
                if logic_kettle != mt.id or actor_id not in (mt.agitator, mt.heater):
                    return False
            elif actor_id != mt.agitator:
                return False
            if logic_kettle is None and presence.pressed() == "actor":
                await self.confirm_strike_moved("pump")
                return False
            if self.strike_move_confirmed():
                return False
            self._tell_wort_held("The mash tun's AUTO" if logic_kettle is not None else "A step or a plugin")
            return True
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not check whether %s may switch on: %s", actor_id, e)
            return False

    def _tell_wort_held(self, who):
        """The "Wort pump held" prompt, with Water's in, kept for every page until the water is in or the brew
        starts over; logged as it goes up."""
        if self.__dict__.get("_wort_prompt") is not None:
            logging.debug("%s wanted to start the wort pump: still held", who)
            return
        logging.warning("%s wanted to start the wort pump before the brewer said the strike water is in the mash "
                        "tun: the wort pump stays off, and nothing heats the mash through the coil", who)

        async def water_in():
            await self.confirm_strike_moved("button")

        try:
            self._wort_prompt = self.cbpi.notify(
                self.WORT_HELD_TITLE, self.WORT_HELD_TEXT.format(who), NotificationType.WARNING,
                action=[NotificationAction(self.WATER_IN, water_in)], keep=True) or "said"
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not tell the brewer the wort pump is held: %s", e)

    def _retire_wort_prompt(self):
        prompt, self._wort_prompt = self.__dict__.get("_wort_prompt"), None
        if prompt in (None, "said"):
            return
        retire = getattr(getattr(self.cbpi, "notification", None), "retire", None)
        if callable(retire):
            try:
                retire(prompt)
            except Exception as e:  # noqa: BLE001
                logging.debug("Could not take the wort pump prompt down: %s", e)

    def _forget_strike_move(self):
        """Starting over, or another brew: the strike water isn't in the mash tun until the brewer says so again."""
        basic = getattr(self, "basic_data", None)
        if isinstance(basic, dict):
            basic.pop(self.STRIKE_MOVED_KEY, None)
        self.__dict__["_self_next_refused"] = {}
        self._retire_wort_prompt()
    # whatever is loaded starts, as it always has.
    _ANY_RECIPE = object()

    def _loaded_name(self):
        """The loaded recipe's name, or None: nothing loaded, or no name."""
        if not getattr(self, "profile", None):
            return None
        basic = self.basic_data if isinstance(self.basic_data, dict) else {}
        name = basic.get("name")
        return name if isinstance(name, str) and name.strip() else None

    async def start(self, by_brewer=True, steps=_ANY_RECIPE):
        """Start the next step - resuming a stopped one first.

        `by_brewer` is True when the brewer asked (Start, Resume, Next on a
        profile with nothing running, the button on a hold), and False when the
        profile is moving on by itself: a step finished, or one was skipped. A
        step that waits for the brewer (_brewer_wait - a boil) is then held
        rather than started, so it never begins heating unattended.

        `steps` is what START on the brew-day screen sends (POST /step2/start):
        the ids of every step of the recipe it was pressed on, in order. Given,
        nothing starts unless they are exactly the loaded profile's ids, in
        their order, and nothing new starts while a step runs: StartRefused,
        "recipe_changed" or "already_running", checked in that order. A
        recipe's ids stay the same through a hold, a stop, a failed step, a
        reset and a restart; adding, deleting or moving a step changes them,
        and so does loading a recipe, the same one included. Both checks come
        before the first await here, so no other request can load a recipe or
        start a step between a check and the start.
        """

        if steps is not self._ANY_RECIPE:
            loaded = [item.id for item in self.profile]
            if steps != loaded:
                name = self._loaded_name()
                now = "'{}'".format(name) if name else "it has no name" if loaded else "nothing is loaded"
                logging.warning("Nothing started: START was pressed on a recipe that isn't the one "
                                "loaded now (%s)", now)
                raise StartRefused("recipe_changed", name, loaded)

        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Steps already running")
            if steps is not self._ANY_RECIPE:
                raise StartRefused("already_running", self._loaded_name(),
                                   [item.id for item in self.profile])
            return

        # A failed step is where the profile stopped, and the brewer's Start
        # retries it - what the failure notice tells them to do. Passing over
        # it to the next step not yet started, as the brewer, would begin the
        # boil heating at once with no question after a failed mash out.
        # Moving on by itself, the profile never passes a failed step.
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
        # brew - a phone, or the monitor reloaded - is shown it too, with an OK
        # to answer it, rather than a line saying "waiting for you" and a title
        # bar saying "No alarms". Until OK, Reset or another recipe.
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
        # The brewer's NEXT (POST /step2/next) or his answer to a question, or something asking by itself
        # (cbpi.api.presence): a step holding the water moves on only on his.
        by_person = presence.pressed() is not None
        active = self.find_by_status(StepState.ACTIVE)
        step = active
        if step is not None:
            if step.instance is not None and getattr(step.instance, "task", None):
                if by_person:
                    # For a step whose own next() doesn't reach CBPiStep.next, which notes the press itself.
                    step.instance._next_pressed = True
                await step.instance.next()
            else:
                # An ACTIVE step with no task behind it is a violated invariant,
                # normally left by a restart. Returning success here without
                # changing anything would be worse than a crash: the brewer
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

        # A failed step is skipped like a stopped one. Falling through to the
        # start() below, as the brewer, would pass over it and begin the next
        # step - a boil, heating with no question.
        step = self.find_by_status(StepState.STOP) or self.find_by_status(StepState.ERROR)
        if step is not None:
            if step.instance is not None:
                # A stopped or failed step waits for the brewer: only his press skips it.
                if not by_person:
                    if self.holds_water_for_brewer(step):
                        self._said_self_next(step)
                    else:
                        self._refuse_unpressed_next(step, skip=True)
                    return
                if self.holds_water_for_brewer(step):
                    self._finished_by_press(step)
                elif self._on_mash_tun(step):
                    self._confirm_strike_moved("next")
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
            # Next means go on, so start it - on his press only.
            if not by_person:
                self._refuse_unpressed_next(self.find_by_status(StepState.INITIAL), skip=False)
                return
            await self.start()
        else:
            logging.info("No Step is running")

    #: Said, as a warning titled with the step's name, when something asks the profile to start a step that waits for
    #: the brewer (not begun, or held before it - the boil's "Ready to boil?") without his press.
    UNPRESSED_START_TEXT = "Something tried to start {} by itself; it waits for you. Press NEXT when you're ready."
    #: Said when something asks to skip a stopped or failed step without his press.
    UNPRESSED_SKIP_TEXT = ("Something tried to skip {} by itself; it waits for you. Press START to carry on, or NEXT "
                           "to skip it.")

    @staticmethod
    def _asked_by():
        """Where the next() being refused came from, for the log: the first caller outside this controller, as
        "module.function", or "unknown"."""
        frame = inspect.currentframe()
        try:
            here = frame.f_code.co_filename
            while frame is not None and frame.f_code.co_filename == here:
                frame = frame.f_back
            if frame is None:
                return "unknown"
            return "%s.%s" % (frame.f_globals.get("__name__", "?"), frame.f_code.co_name)
        finally:
            del frame

    def _refuse_unpressed_next(self, step, skip):
        """next() not from a person's press while the profile waits for the brewer at `step`: before it (not begun,
        or held before it) or at it, stopped or failed. Nothing starts or is skipped. A WARNING, and a warning kept
        for every page while that wait lasts, said once a wait; asked again meanwhile, logged quietly."""
        status = step.status
        prompts = self.__dict__.get("_kept_prompts") or {}
        if any(kind == "unpressed-next" and step_id == step.id for kind, step_id, _ in prompts.values()):
            logging.debug("Asked again to move on to '%s' without the brewer's press: refused", step.name)
            return
        logging.warning("'%s' waits for the brewer, %s: %s asked to %s it without his press, so nothing %s",
                        step.name, "stopped" if status == StepState.STOP else "failed" if status == StepState.ERROR
                        else "not started", self._asked_by(), "skip" if skip else "start",
                        "is skipped" if skip else "starts")
        text = self.UNPRESSED_SKIP_TEXT if skip else self.UNPRESSED_START_TEXT
        try:
            self._ask_kept("unpressed-next", step,
                           lambda s: s.status == status and self.find_by_status(StepState.ACTIVE) is None,
                           step.name or "Brew Steps", text.format(step.name or "the next step"),
                           NotificationType.WARNING, action=[NotificationAction("OK")])
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not tell the brewer that something tried to move on to '%s': %s", step.name, e)

    async def resume(self):
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
                # Paused: no pump starts again by itself when it carries on (pump_starts).
                self._forget_pump_starts()
                self._tell("stopped", step)

                await self.save()
            except Exception as e:
                logging.error("Failed to stop step - Id: %s" % step.id)

    # Told to whoever asks for a reset while a step runs (http_reset's 409).
    RESET_WHILE_RUNNING = "A step is running. Stop it before resetting the recipe."
    LOAD_WHILE_RUNNING = "{} is running. Stop it before loading another recipe."
    CLEAR_WHILE_RUNNING = "{} is running. Stop it before unloading the recipe."

    def running_step(self):
        """The step running now, or None. While one runs, a reset, a load and a clear are refused."""
        return self.find_by_status(StepState.ACTIVE)

    def _refuse_while_running(self, title, sentence):
        """Raise StepRunning, after telling the brewer why, when a step runs. An info: the brew carries on as it was,
        and nothing waits for an answer."""
        running = self.running_step()
        if running is None:
            return
        name = running.name or "A step"
        detail = sentence.format(name)
        logging.warning("%s: %s", title, detail)
        self.cbpi.notify(title, detail, NotificationType.INFO)
        raise StepRunning(name, detail)

    async def reset_all(self):
        """Every step back to not started. Returns False, changing nothing, while a step runs."""
        if self.find_by_status(StepState.ACTIVE) is not None:
            logging.error("Please stop before reset")
            return False

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
            # already given. Left behind, the next brew's
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
        self._forget_strike_move()
        self._forget_pump_starts()
        # Starting over: no question about the brew before still stands.
        self._retire_prompts(everything=True)
        self._tell("reset")
        await self.save()
        self.push_udpate()
        return True

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
            # When and how the brewer said the strike water is in the mash tun, this brew; None until he has.
            "strike_moved": (self.basic_data.get(self.STRIKE_MOVED_KEY)
                             if isinstance(self.basic_data, dict) else None),
        }

    async def move(self, id, direction: StepMove):
        """Swap a step with its neighbour above (-1) or below (1). Nothing moves past either end: a page whose list
        is out of date can ask for that, and the profile's last step would otherwise take the first one's place."""
        index = self.get_index_by_id(id)
        if direction not in [-1, 1]:
            self.logger.error("Cant move. Direction 1 and -1 allowed")
            return
        if index is None or not 0 <= index + direction < len(self.profile):
            self.logger.warning("Step %s not moved: it is already at that end of the brew steps", id)
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
            # A step holding the water moves on only on the brewer's press (CBPiStep.next refuses anything else),
            # or by itself to a step that holds the water too; one that got round that is started again to hold it.
            if self.holds_water_for_brewer(step_current):
                if getattr(step, "_next_pressed", False):
                    self._finished_by_press(step_current)
                elif not self._hands_to_holder(step_current):
                    self._refuse_finished_hold(step_current)
                    return
            elif getattr(step, "_next_pressed", False) and self._on_mash_tun(step_current):
                # The brewer moving on from a mash tun step himself: the mash is in it.
                self._confirm_strike_moved("next")
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
        self._log_active_step()
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

    def _log_active_step(self):
        """Log the number of the step now active - 1 for the first, 0 for none - as "steps.active" each time it
        changes, beside every step_update: a past brew's graph on Figures draws its numbered step marks from it, as
        Brew Day's graphs do from the steps they see. log_change writes it through log_control_data, only when it
        differs from the last number logged."""
        try:
            log = getattr(self.cbpi, "log", None)
            if log is None or not hasattr(log, "log_change"):
                return
            active = next((i + 1 for i, item in enumerate(self.profile or [])
                           if item.status == StepState.ACTIVE), 0)
            log.log_change("steps.active", active)
        except Exception as e:  # noqa: BLE001 - a step mark never stops the brew
            logging.warning("Could not log the active step: %s", e)

    def add_listener(self, fn):
        """Tell `fn` about the brew steps as they change: fn(event, snapshot).

        For a plugin that follows the brew without running it - BrewScribe's
        reminders, such as "Check mash pH again before mash-out or transfer",
        need to know when a rest will end, and polling once a real second
        cannot tell that on a simulator running 60 times faster.

        `event` is one of LISTENER_EVENTS, and `snapshot` is snapshot(event),
        a copy of its own. A plain function is called on the event loop right
        after the change, so it must return quickly; an `async def` is run as a
        task of its own, just after. A listener must not start, stop, move on
        or reset the brew steps. A plugin that only follows the brew, as
        BrewScribe does, switches nothing either; the core's hltpump extension,
        which keeps the HLT pump's rules, switches that pump off from a task of
        its own as the runoff or the recipe ends.
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
        # A SpargeStep is the runoff whatever it is called.
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

        First-wort hops go in before the wort reaches the boil kettle, and the
        profile holds before the boil, so the boil's own start is too late to
        ask. A step
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
            # The brewer starting a mash tun step himself - START, Resume - says the strike water is in it.
            if by_brewer and self._on_mash_tun(step):
                self._confirm_strike_moved("start")
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
            self._sparge_additions_at_runoff(step)
        except Exception as e:
            self.cbpi.notify(
                "Error",
                "Can't start the step. Check it on the Brew Steps page.",
                NotificationType.ERROR,
            )
            logging.error("Failed to start step %s" % step)

    async def call_action(self, id, action, parameter) -> bool:
        """Run a declared action on a step. True if it ran; False if it didn't.

        False when no step has that id, the step doesn't declare that action,
        or the action raised, a refusal included. POST /step2/action/{id}
        answers 400 on False, as the actor, sensor and kettle endpoints do with
        BasicController.call_action. It runs the action through run_action,
        which tells a refusal from the rest.
        """
        try:
            return await self.run_action(id, action, parameter)
        except ActionRefused:
            return False

    async def run_action(self, id, action, parameter) -> bool:
        """call_action, with a refusal told apart: True if it ran; False if it didn't, as call_action.

        Raises ActionRefused when the action says no with a reason: it raised ValueError, and the reason is its
        words. That is an answer for the brewer, not a fault, so it is logged as information. A ValueError with no
        words gives no reason, and is False like any other exception.
        """
        logging.info("Step Controller - call all Action {} {}".format(id, action))
        try:
            item = self.find_by_id(id)
            if item is None:
                logging.error("Step Controller - no such step %s", id)
                return False
            method = resolve_action(item.instance, action)
            if method is None:
                logging.error(
                    "Step Controller - refused undeclared action %r on %s - "
                    "only methods decorated with @action can be called",
                    action,
                    id,
                )
                return False
            await method(**normalize_action_parameters(parameter))
            return True
        except ValueError as e:
            reason = str(e).strip()
            if reason:
                logging.info("Step Controller - %r on %s refused: %s", action, id, reason)
                raise ActionRefused(reason) from None
            logging.error(
                "Step Controller -Failed to call action on {} {} {!r}".format(
                    id, action, e
                )
            )
            return False
        except Exception as e:
            logging.error(
                "Step Controller -Failed to call action on {} {} {}".format(
                    id, action, e
                )
            )
            return False

    async def load_recipe(self, data, book_file=None, book_modified=None):
        """Make `data` the brew steps, every step waiting. Raises StepRunning, changing nothing, while a step runs.

        From the Recipe Book (RecipeController.brew), `book_file` is the book recipe's file, and `book_modified` its
        modified time as it was read: both kept in the basic data, with when it was loaded (BOOK_FILE_KEY,
        LOADED_AT_KEY, BOOK_MODIFIED_KEY). Whatever `data` holds of another brew (BREW_KEYS) goes, so a new brew
        starts with nothing answered. The recipe's own basic data comes with it: a BeerXML import's file and its
        recipe's name too (SOURCE_KEYS).
        """
        self._refuse_while_running("Recipe not loaded", self.LOAD_WHILE_RUNNING)
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
        basic = data.setdefault("basic", {})
        basic.pop(self.MASH_PH_REMINDER_KEY, None)
        for key in self.BREW_KEYS:
            basic.pop(key, None)
        if book_file is not None:
            basic[self.BOOK_FILE_KEY] = book_file
            basic[self.LOADED_AT_KEY] = clock.now()
            if book_modified is not None:
                basic[self.BOOK_MODIFIED_KEY] = book_modified
        # In one write: a recipe from the book replaces the whole profile,
        # and a write broken part-way would leave an unreadable file, which
        # load() would then set aside for an empty profile.
        atomic_write_json(self.path, data, indent=4, sort_keys=True)
        self._held_step_id = None
        self.load()
        self._forget_strike_move()
        self._forget_pump_starts()
        self._retire_prompts(everything=True)
        self.push_udpate(complete=True)
        self._tell("loaded")

    async def clear(self):
        """Empty the brew steps. Raises StepRunning, changing nothing, while a step runs."""
        self._refuse_while_running("Recipe not unloaded", self.CLEAR_WHILE_RUNNING)
        try:
            await self.shutdown()
        except:
            pass

        data = dict(basic=dict(), steps=[])
        with open(self.path, "w") as file:
            json.dump(data, file, indent=4, sort_keys=True)

        self._held_step_id = None
        self.load()
        self._forget_strike_move()
        self._forget_pump_starts()
        self._retire_prompts(everything=True)
        self.push_udpate(complete=True)
        self._tell("loaded")

    #: In the basic data of a recipe whose import couldn't give every boil
    #: addition a reminder: the ones left without, [{"name", "minutes"}, ...]
    #: (UploadController keeps them, and the pre-brew check names them). Part
    #: of the recipe, like its name: gone when the profile is cleared or another
    #: recipe is loaded, kept through a reset, a restart and the recipe book.
    NO_REMINDER_KEY = "boil_additions_without_reminder"

    def additions_without_reminder(self):
        """The loaded recipe's boil additions that have no reminder:
        [{"name", "minutes"}, ...], longest first, each named as the import's
        warning named it, its minutes before the end of the boil.

        Empty for a recipe built by hand: the boil step's own Hop_1 to Hop_6
        are all its additions. An entry without a number of minutes is left
        out; a name the recipe didn't give is None.
        """
        basic = self.basic_data if isinstance(self.basic_data, dict) else {}
        kept = basic.get(self.NO_REMINDER_KEY)
        found = []
        for entry in kept if isinstance(kept, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                minutes = float(entry.get("minutes"))
            except (TypeError, ValueError):
                continue
            if not math.isfinite(minutes):
                continue
            name = entry.get("name")
            found.append(dict(name=None if name in (None, "") else str(name), minutes=minutes))
        return found

    #: In the basic data of a BeerXML import, beside its mash_numbers (UploadController reads them): what goes into
    #: the mash and into the sparge water - the water agents and acids, and any other BeerXML MISC whose USE is Mash or
    #: Sparge - each {"name", "type", "use", "amount", "label", "remind"}, the amount as BeerSmith shows it
    #: (DISPLAY_AMOUNT), the label the reminders name it by ("5.00 g Gypsum"), and whether they do (REMIND_KEY); the
    #: pH of the recipe's mash profile, {"value", "from": "mash_profile"}; and its yeasts. Part of the recipe, like its
    #: name: LOAD, SAVE TO BOOK and the book keep them, and an UPDATE from BeerSmith brings them up to date
    #: (RecipeController.update_from_import, take_book_figures). The dough-in's MashInStep names the mash's water
    #: salts and acids in its question for the malt (dough_in_words), and the runoff's start the sparge water's
    #: (_sparge_additions_at_runoff).
    MASH_ADDITIONS_KEY = "mash_additions"
    SPARGE_ADDITIONS_KEY = "sparge_additions"
    MASH_PH_KEY = "mash_ph"
    YEASTS_KEY = "yeasts"
    RECIPE_FACTS = (MASH_ADDITIONS_KEY, SPARGE_ADDITIONS_KEY, MASH_PH_KEY, YEASTS_KEY)

    #: In each of those additions: True when the reminders name it - a water salt or acid (is_water_treatment) - and
    #: False for the rest, rice hulls or a fining, kept for a plugin's notes all the same. An addition kept before
    #: there was one is judged as the import would (reminded).
    REMIND_KEY = "remind"

    @staticmethod
    def is_water_treatment(type, name):
        """Whether a BeerXML MISC of TYPE `type` named `name` is a water salt or acid, the only mash and sparge
        additions the brew reminds of: TYPE "Water Agent" - BeerSmith's water salts, acids and Campden tablets - or a
        name with the word acid in it ("Lactic Acid", "Citric Acid"), but not acid malt, a grain."""
        if str(type or "").strip().lower() == "water agent":
            return True
        named = str(name or "")
        return bool(re.search(r"\bacid\b", named, re.I)) and not re.search(r"\bmalt\b", named, re.I)

    @classmethod
    def reminded(cls, addition):
        """Whether the reminders name `addition`, an entry of mash_additions or sparge_additions: its REMIND_KEY, or
        for one kept without it, is_water_treatment."""
        said = addition.get(cls.REMIND_KEY)
        if isinstance(said, bool):
            return said
        return cls.is_water_treatment(addition.get("type"), addition.get("name"))

    #: On the runoff step that asked for the sparge water's additions: asked once a brew, not again when that step or
    #: another runoff step starts, nor after a restart. Underscored: cleared by a reset and never kept in a recipe.
    SPARGE_ASKED_PROP = "_sparge_additions_asked"

    def _additions_words(self, key):
        """'5.00 g Gypsum, 3.00 g Calcium Chloride': the loaded recipe's water salts and acids under `key`
        (MASH_ADDITIONS_KEY or SPARGE_ADDITIONS_KEY), in recipe order, each by its label; None when it has none. The
        rest of its additions there - rice hulls, a fining - are left out (reminded)."""
        basic = self.basic_data if isinstance(self.basic_data, dict) else {}
        items = basic.get(key)
        labels = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not self.reminded(item):
                continue
            label = str(item.get("label") or item.get("name") or "").strip()
            if label:
                labels.append(label)
        return ", ".join(labels) or None

    def dough_in_words(self, step_id, again=False):
        """What the question for the malt adds for the loaded recipe's water salts and acids for the mash: "Add to the
        mash: 5.00 g Gypsum, 3.00 g Calcium Chloride." - asked by the dough-in's MashInStep only: the first that
        doesn't hold the strike water for the brewer (holds_water_for_brewer: one on the HLT), else the first, so a
        recipe with a second never asks twice. None for any other step, or a recipe with none (MashInStep.
        _ask_for_malt).

        `again`: the step has asked for the malt before and asks again - started after a Stop, after a restart, or
        at a new strike temperature - when they may be in already: "If you haven't yet, add to the mash: ...", so
        nobody adds them twice."""
        mashins = [s for s in getattr(self, "profile", None) or [] if s.type == "MashInStep"]
        first = next((s for s in mashins if not self.holds_water_for_brewer(s)), mashins[0] if mashins else None)
        if first is None or first.id != step_id:
            return None
        words = self._additions_words(self.MASH_ADDITIONS_KEY)
        if not words:
            return None
        return ("If you haven't yet, add to the mash: {}." if again else "Add to the mash: {}.").format(words)

    def _sparge_additions_at_runoff(self, step):
        """Ask for the sparge water's salts and acids as the runoff starts, beside the first-wort hops
        (_first_wort_at_runoff): "Add to the sparge water: 2.00 ml Lactic Acid." Once a brew (SPARGE_ASKED_PROP).
        Kept for a page opened later, until answered or the step is done. Nothing for a recipe with none, even one
        with other sparge additions (_additions_words)."""
        try:
            if not self._collects_wort(step):
                return
            words = self._additions_words(self.SPARGE_ADDITIONS_KEY)
            if not words:
                return
            if any((s.props or {}).get(self.SPARGE_ASKED_PROP) for s in self.profile):
                return
            step.props[self.SPARGE_ASKED_PROP] = True
            self._ask_kept(
                "sparge-additions", step, lambda runoff: runoff.status != StepState.DONE,
                "Sparge water", "Add to the sparge water: {}.".format(words),
                NotificationType.WARNING, action=[NotificationAction("Added")],
            )
        except Exception as e:  # noqa: BLE001 - a reminder never stops the brew
            logging.warning("Could not ask for the sparge water's additions at '%s': %s",
                            getattr(step, "name", "?"), e)

    #: Where an import came from, in a recipe the book keeps for it - as
    #: RecipeController.IMPORTED_FROM. Not part of a recipe the brewer saves.
    IMPORTED_FROM_KEY = "imported_from"

    #: In the brew steps' basic data, from LOAD (load_recipe) and SAVE TO BOOK (savetobook): the Recipe Book file
    #: the steps are a copy of; when they were loaded, on CraftBeerPi's clock (cbpi.api.clock); and that book
    #: file's modified time when it was read or written, on the file system's clock, as GET /recipe/'s modified
    #: is, so a page can tell the book's copy has been changed since. The steps' own, not a recipe's: book_data()
    #: leaves them out, and loading another recipe or clearing the steps drops them.
    BOOK_FILE_KEY = "book_file"
    LOADED_AT_KEY = "loaded_at"
    BOOK_MODIFIED_KEY = "book_modified"
    BOOK_KEYS = (BOOK_FILE_KEY, LOADED_AT_KEY, BOOK_MODIFIED_KEY)

    #: In the brew steps' basic data: Brew Day's answer to where the grain spent the night, for this brew - "garage",
    #: so the boil kettle's probe stands for it, or "indoors", at the temperature given - absent while it hasn't been
    #: asked (StrikeController.set_grain). This brew's, as BOOK_KEYS are: a load or unload drops it, and book_data()
    #: leaves it out.
    GRAIN_PLACE_KEY = "grain_place"
    GRAIN_PLACES = ("garage", "indoors")
    #: What belongs to this brew of the steps rather than to the recipe: the strike water said to be in the mash tun
    #: (STRIKE_MOVED_KEY) too.
    BREW_KEYS = BOOK_KEYS + (GRAIN_PLACE_KEY, STRIKE_MOVED_KEY)

    #: In the basic data of a book recipe imported from BeerXML, and of the brew steps loaded from it: the BeerXML
    #: file the book keeps for it, and the NAME of the recipe in that file (RecipeController.SOURCE_KEYS). LOAD
    #: brings them with the recipe, and the brew steps keep them, through SAVE TO BOOK too: they say which file
    #: these steps came from. The book's are the book's: book_data() leaves them out, so a new book recipe has
    #: none, and SAVE TO BOOK in place of a recipe keeps the ones it has (savetobook).
    SOURCE_XML_KEY = "source_xml"
    SOURCE_XML_RECIPE_KEY = "source_xml_recipe"
    SOURCE_KEYS = (SOURCE_XML_KEY, SOURCE_XML_RECIPE_KEY)

    #: A BrewStart, and how Brew Day gives its day for one brew: as a date (Mode Date; DateTime in older forms). The
    #: Recipe Book gets it as Tomorrow, at the same time (book_data).
    BREWSTART_TYPE = "BrewStart"
    BREWSTART_DATE_MODES = ("Date", "DateTime")
    BREWSTART_RECIPE_MODE = "Tomorrow"

    def book_data(self):
        """The brew steps as the recipe book keeps them: the recipe, not this run.

        What SAVE TO BOOK writes. Not the mash pH reminder, which belongs to
        this brew; not which book recipe the steps are a copy of, or when they
        were loaded, or where tonight's grain spent the night (BREW_KEYS); not
        the BeerXML file a book recipe was imported with (SOURCE_KEYS), which
        savetobook gives back to a recipe replaced in place; and not where an
        import came from: a copy the brewer saves is the brewer's own, and an
        import of the same name never replaces it
        (RecipeController.keep_import).

        Nor three things Brew Day sets in the steps for one brew, which go in
        as the recipe had them, while the brew steps keep them as they are:
        - a BrewStart's day given as a date is Tomorrow, at the same time,
          with no StartDate: by the next brew the date has passed;
        - the strike water step's Temp and BrewStart's Ready_Temp, while they
          hold the figure USE wrote, are what they were before the first USE
          (StrikeController.recipe_temps): USE is worked out for each brew;
        - a BrewStart's Grain_Temp is blank: Brew Day asks where the grain is
          every brew.
        """
        basic = copy.deepcopy(self.basic_data)
        basic.pop(self.MASH_PH_REMINDER_KEY, None)
        basic.pop(self.IMPORTED_FROM_KEY, None)
        for key in self.SOURCE_KEYS:
            basic.pop(key, None)
        for key in self.BREW_KEYS:
            basic.pop(key, None)
        steps = list(map(lambda item: item.to_dict(), self.profile))
        before_use = self._strike_before_use()
        for step in steps:
            props = step.get("props")
            if not isinstance(props, dict):
                continue
            props.update(before_use.get(step.get("id")) or {})
            if step.get("type") != self.BREWSTART_TYPE:
                continue
            if str(props.get("Mode") or "").strip() in self.BREWSTART_DATE_MODES:
                props["Mode"] = self.BREWSTART_RECIPE_MODE
                props["StartDate"] = ""
            if "Grain_Temp" in props:
                props["Grain_Temp"] = ""
        return dict(basic=basic, steps=steps)

    def _strike_before_use(self):
        """{step id: {prop: temperature}}: the strike water before the first USE, from the strike water helper
        (StrikeController.recipe_temps); {} without one, or when it can't say."""
        temps = getattr(getattr(self.cbpi, "strike", None), "recipe_temps", None)
        if not callable(temps):
            return {}
        try:
            found = temps(self.profile)
        except Exception:  # noqa: BLE001 - the book then gets the strike water as the steps have it
            logging.exception("SAVE TO BOOK: could not tell what the strike water was before USE")
            return {}
        return found if isinstance(found, dict) else {}

    #: Why SAVE TO BOOK can't replace a book recipe: it isn't there.
    NOT_IN_BOOK = ("That recipe is no longer in the Recipe Book: it may have been deleted on another screen. "
                   "Nothing was saved.")
    #: Why a new book recipe needs a name, in the page's words.
    NEEDS_A_NAME = "Give it a name first: the Recipe Book lists recipes by name."

    @staticmethod
    def _book_file_id(file):
        """Whether `file` can be a Recipe Book file's id: a plain name, never a path."""
        return (isinstance(file, str) and file.strip() == file and file not in ("", ".", "..")
                and not file.startswith(".") and os.path.basename(file) == file
                and "/" not in file and "\\" not in file)

    @classmethod
    def _book_source(cls, path):
        """{source_xml, source_xml_recipe} as the book recipe at `path` has them (SOURCE_KEYS), or {}."""
        try:
            with open(path) as file:
                basic = (yaml.load(file, Loader=yaml.FullLoader) or {}).get("basic") or {}
        except Exception:  # noqa: BLE001 - the recipe is then saved without them
            logging.exception("SAVE TO BOOK: could not read %s to keep its BeerXML file", path)
            return {}
        if not isinstance(basic, dict):
            return {}
        return {key: basic[key] for key in cls.SOURCE_KEYS if key in basic}

    async def savetobook(self, file=None, name=None):
        """Keep the brew steps in the Recipe Book (book_data). -> the book file's id.

        - `file`: in place of that book recipe, named as the steps are;
        - `name`: as a new book recipe of that name, which the steps are then called too;
        - neither: as a new book recipe named as the steps are, as SAVE TO BOOK always did.

        A recipe replaced in place keeps the BeerXML file it was imported with (SOURCE_KEYS); a new one has none.
        The brew steps keep theirs either way.

        With `file` or `name`, the steps then know which book recipe they are a copy of (BOOK_FILE_KEY,
        BOOK_MODIFIED_KEY), saved and sent to every page on mash_profile_update. Raises BookRefused, changing
        nothing: 400 bad_body for both, a `file` that can't be a book file's id, or a `name` that is blank or not
        text; 404 not_in_book for a `file` the book doesn't have.
        """
        if file is None and name is None:
            book_id = shortuuid.uuid()
            path = os.path.join(
                self.cbpi.config_folder.get_file_path("recipes"), "{}.yaml".format(book_id)
            )
            data = self.book_data()
            with open(path, "w") as out:
                yaml.dump(data, out)
            self.push_udpate()
            return book_id
        if file is not None and name is not None:
            raise BookRefused(400, "bad_body", detail="Send file to replace a book recipe, or name to save the "
                                                      "steps as a new one, not both.")
        data = self.book_data()
        if file is not None:
            if not self._book_file_id(file):
                raise BookRefused(400, "bad_body", detail="file is the id of a recipe in the Recipe Book, as "
                                                          "GET /recipe/ lists it.")
            book_id = file
            path = self.cbpi.config_folder.get_recipe_file_by_id(book_id)
            if not os.path.isfile(path):
                raise BookRefused(404, "not_in_book", detail=self.NOT_IN_BOOK, file=book_id)
            data["basic"].update(self._book_source(path))
        else:
            if not isinstance(name, str) or not name.strip():
                raise BookRefused(400, "bad_body", detail=self.NEEDS_A_NAME)
            name = name.strip()
            data["basic"]["name"] = name
            book_id = shortuuid.uuid()
            path = self.cbpi.config_folder.get_recipe_file_by_id(book_id)
        atomic_write_text(path, yaml.dump(data, indent=4, sort_keys=True))
        if name is not None:
            self.basic_data["name"] = name
        self.basic_data[self.BOOK_FILE_KEY] = book_id
        self.basic_data[self.BOOK_MODIFIED_KEY] = os.path.getmtime(path)
        await self.save()
        # The name and the book recipe are basic data, which pages are sent only on mash_profile_update.
        self.cbpi.ws.send(dict(topic="mash_profile_update", data=self.get_state()))
        return book_id

    #: The mash figures of a BeerXML import, in the basic data (UploadController.MASH_NUMBERS_KEY).
    MASH_NUMBERS_KEY = "mash_numbers"

    async def take_book_figures(self, book_file, numbers, source, modified=None, facts=None):
        """UPDATE from an import (UploadController.import_recipe) of the book recipe the brew steps were loaded from
        (BOOK_FILE_KEY is `book_file`): the steps take its mash figures `numbers` and its BeerXML file `source`
        ({source_xml, source_xml_recipe}, SOURCE_KEYS) into their basic data at once, nothing loaded again. No step's
        status changes, so it works while a step runs or BrewStart waits; the one setting that does is a BrewStart
        not begun whose Ready_Temp still holds BeerSmith's old strike, which takes the new one
        (RecipeController.strike_follows_update).

        `facts`, {key: value} for RECIPE_FACTS: the import's mash and sparge additions, mash pH and yeasts, each
        taken as the import has it, or dropped where it has none; None leaves them as they are.

        `modified`, (the book file's modified time before the update, after it): when the steps were as the book had
        them (BOOK_MODIFIED_KEY not older than before), they are counted as the book's copy after it too, so a page
        doesn't offer to load them again for figures they already have. -> whether the steps were that recipe's."""
        basic = self.basic_data if isinstance(self.basic_data, dict) else None
        if basic is None or not isinstance(book_file, str) or not book_file or basic.get(self.BOOK_FILE_KEY) != book_file:
            return False
        if numbers:
            follow = getattr(getattr(self.cbpi, "recipe", None), "strike_follows_update", None)
            if callable(follow):
                try:
                    follow(self.profile, basic.get(self.MASH_NUMBERS_KEY), numbers)
                except Exception as e:  # noqa: BLE001 - the figures are taken all the same
                    logging.warning("UPDATE: BrewStart's water temperature could not follow BeerSmith's: %s", e)
            basic[self.MASH_NUMBERS_KEY] = copy.deepcopy(numbers)
        if isinstance(facts, dict):
            for key in self.RECIPE_FACTS:
                if facts.get(key):
                    basic[key] = copy.deepcopy(facts[key])
                else:
                    basic.pop(key, None)
        for key in self.SOURCE_KEYS:
            basic.pop(key, None)
        basic.update({key: source[key] for key in self.SOURCE_KEYS if isinstance(source, dict) and key in source})
        if modified is not None:
            was, now = modified
            try:
                current = float(basic.get(self.BOOK_MODIFIED_KEY)) >= float(was) - 0.001
            except (TypeError, ValueError):
                current = False
            if current and now is not None:
                basic[self.BOOK_MODIFIED_KEY] = now
        await self.save()
        self.cbpi.ws.send(dict(topic="mash_profile_update", data=self.get_state()))
        return True
