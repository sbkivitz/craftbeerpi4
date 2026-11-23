"""CraftBeerPi's clock set back to the computer's time while the server runs.

On a simulator the brewing clock (cbpi.api.clock) runs SIM_TIME_SCALE times as
fast as the computer's. An hour at 30x takes it thirty hours ahead, and a change
of speed alone (clock.set_scale) carries on from there: after a 30x run set back
to 1x, a BrewStart for "Today 23:15" plans for the day the run had reached. So
when a simulator's speed changes, cbpi4-simvessel calls reset(), and the clock
goes back to the computer's date and time and runs at the new speed from there.

Everything that keeps a time on the clock's now() moves with it by as much, at
once, before anything else runs, so the brew carries on as it was, each
countdown with as long to go:

- every Timer (a step's rest, a boil, an alarm sensor) - Timer.clock_moved_all;
- the brew steps: until when a step may carry on by itself after a restart
  (_resume_until), when it first started (_started_at), the stalled-heating
  watch and when progress was last written, the boil's readings for its
  auto-timer, loaded_at, the mash pH reminder - StepController.clock_moved;
- when each brew-day timer ran out - TimersController.clock_moved; their
  countdowns, and the rest timer's, are on clock.monotonic(), which doesn't jump;
- the alarms for heating by hand and for AUTO with no target - their extensions'
  clock_moved;
- each running kettle logic with a clock_moved of its own. One without is
  stopped and started again, as AUTO off and on would
  (KettleController.restart_after_clock_reset).

BrewStart plans again from its settings, as START does, Today and Tomorrow
counted from the clock as it now is (StepController.replan_after_clock_reset).

A jump shorter than QUIET_S - a change of speed while the clock was where the
computer's is, at 1x - moves those times, and starts nothing again.

The sensor logs with rows stamped ahead of the clock as it now is are archived,
as a simulator does at start-up with an earlier accelerated run's, so no graph
goes back in time (LogController.archive_logs_ahead).

Every screen is told at once, on the websocket: {"topic": "clock", "data":
{"now", "scale", "was": {"now", "scale"}, "moved"}} - the clock's seconds now and
its speed, as POST /log/window gives them, the same before the reset, and how far
now() moved (negative: back). A notification says it in words, the time of day
as CLOCK_FORMAT shows it.

Nothing on a rig calls it: only cbpi4-simvessel sets the speed.
"""

import asyncio
import logging
from datetime import datetime

from cbpi.api import clock
from cbpi.api.dataclasses import NotificationType
from cbpi.api.timefmt import time_of_day
from cbpi.api.timer import Timer

logger = logging.getLogger(__name__)


def clock_time(seconds, source=None):
    """'Sun 22 Nov 11:18 PM', or 'Sun 22 Nov 23:18': a moment on the brewing clock, as the brewer is told it, its
    time of day as CLOCK_FORMAT shows it (cbpi.api.timefmt; `source` is CraftBeerPi, or the setting's value)."""
    moment = datetime.fromtimestamp(float(seconds))
    return "{:%a} {} {:%b} {}".format(moment, moment.day, moment, time_of_day(moment, source))


def speed(scale):
    """'30×', '1×', '0.5×': how fast the brewing clock runs."""
    return "{:g}\u00d7".format(float(scale))


class ClockController:
    #: The websocket topic every screen hears as the clock is reset.
    TOPIC = "clock"
    #: The notification's title.
    TITLE = "Clock"
    #: Seconds: a jump shorter than this starts no kettle logic, and no BrewStart, again.
    QUIET_S = 1.0
    #: The controllers told clock_moved(seconds), when they have it: what they keep on now().
    CONTROLLERS = ("step", "timers", "resttimer", "strike")

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self._lock = asyncio.Lock()

    async def reset(self, rate):
        """Set the brewing clock back to the computer's date and time, running at
        `rate`, and everything that keeps a time on it with it (see the module's
        notes). Returns what every screen was told, with "archived": the logs
        archived (LogController.archive_logs_ahead) or None, and "restarted": the
        kettles whose logic started again; or None, changing nothing, when `rate`
        is not a usable speed."""
        async with self._lock:
            was = dict(now=clock.now(), scale=clock.scale())
            moved = clock.reset(rate)
            if moved is None:
                logger.warning("The brewing clock was not reset: %r is not a speed it can run at", rate)
                return None
            reset_at = clock.now()
            restart = self._move(moved)
            restarted = []
            if abs(moved) >= self.QUIET_S:
                restarted = await self._restart_logics(restart)
                await self._replan()
            await self._save_steps()
            # Readings stamped before the reset reach their log through two tasks: let them get there, so none
            # lands in a log started afresh below.
            for _ in range(3):
                await asyncio.sleep(0)
            archived = self._archive(reset_at)
            state = dict(now=clock.now(), scale=clock.scale(), was=was, moved=moved)
            try:
                self.cbpi.ws.send(dict(topic=self.TOPIC, data=state))
            except Exception as e:  # noqa: BLE001 - a screen resyncs within its minute anyway
                logger.warning("Could not tell the screens the clock was reset: %s", e)
            self._tell(dict(state, now=reset_at), archived)
            return dict(state, archived=archived, restarted=restarted)

    def _move(self, moved):
        """Everything that keeps a time on now(), moved by `moved` seconds. No
        await here: nothing else may run between the jump and these. Returns the
        kettles whose logic has to start again."""
        Timer.clock_moved_all(moved)
        for name in self.CONTROLLERS:
            told = getattr(getattr(self.cbpi, name, None), "clock_moved", None)
            if callable(told):
                try:
                    told(moved)
                except Exception as e:  # noqa: BLE001 - the rest still move
                    logger.warning("%s could not move its times with the clock: %s", name, e)
        for extension in list(getattr(getattr(self.cbpi, "plugin", None), "extensions", None) or ()):
            told = getattr(extension, "clock_moved", None)
            if callable(told):
                try:
                    told(moved)
                except Exception as e:  # noqa: BLE001
                    logger.warning("%s could not move its times with the clock: %s", type(extension).__name__, e)
        kettles = getattr(getattr(self.cbpi, "kettle", None), "clock_moved", None)
        if not callable(kettles):
            return []
        try:
            return kettles(moved)
        except Exception as e:  # noqa: BLE001
            logger.warning("The kettles could not move their times with the clock: %s", e)
            return []

    async def _restart_logics(self, ids):
        restart = getattr(getattr(self.cbpi, "kettle", None), "restart_after_clock_reset", None)
        if not ids or not callable(restart):
            return []
        try:
            return await restart(ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not start the kettles' logics again after the clock was reset: %s", e)
            return []

    async def _replan(self):
        replan = getattr(getattr(self.cbpi, "step", None), "replan_after_clock_reset", None)
        if callable(replan):
            try:
                await replan()
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not plan BrewStart again after the clock was reset: %s", e)

    async def _save_steps(self):
        """The brew steps' moved times on disk, and on every screen (StepController.save sends them)."""
        step = getattr(self.cbpi, "step", None)
        if step is None:
            return
        try:
            await step.save()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not save the brew steps' times after the clock was reset: %s", e)

    def _archive(self, now):
        archive = getattr(getattr(self.cbpi, "log", None), "archive_logs_ahead", None)
        if not callable(archive):
            return None
        try:
            return archive(now)
        except Exception as e:  # noqa: BLE001 - never what stops the clock being reset
            logger.warning("Could not archive the sensor logs written ahead of the clock: %s", e)
            return None

    def _tell(self, state, archived):
        message = "CraftBeerPi's clock is back to this computer's time, {}, now running {} (was {}).".format(
            clock_time(state["now"], self.cbpi), speed(state["scale"]), speed(state["was"]["scale"]))
        if archived:
            message += (" The sensor logs written ahead of it, up to {}, were archived so the graphs start again "
                        "from now - zipped ({}) and moved to {}.".format(
                            clock_time(archived["latest"], self.cbpi), ", ".join(archived["zips"]) or "no zip",
                            archived["folder"]))
        logger.info("%s The clock moved %+.1f s.", message, state["moved"])
        try:
            self.cbpi.notify(self.TITLE, message, NotificationType.INFO)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not say the clock was reset: %s", e)
