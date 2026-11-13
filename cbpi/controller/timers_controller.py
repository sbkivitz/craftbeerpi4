"""The brew-day screen's timers: up to four, kept here so every screen shows the same ones.

Scott, 10 Nov 2026, after using the rest timer: "why is it named that? lots of
things use a timer during a brew day. I want an optionally audible alarm and
more attention grabbing but still elegant visual alerting when timer expires.
Also want to have multiple timers in parallel if I click a + or something like
that. Nice to have= ability to name timers". On 11 Nov he approved the
dashboard agent's design: "add the timers that design agent discussed".

A timer has a name ("Timer 1" until it is given one), its own time, and a
state: stopped (ready, at its own time), running, paused, or done (it ran out,
and its alarm waits for an answer). At zero it raises a prompt titled with its
name, with +5 MIN and OK, which every screen keeps until somebody answers it on
any one of them. OK puts the timer back to its own time, stopped, ready to start
again, as a kitchen timer's recall does; +5 MIN runs it for five minutes more.

The screen offers names for a timer from the BREWDAY_TIMER_NAMES setting, a list
separated by commas (Scott: "an easy config setting to choose the default timer
names from the setup").

They run on the brewing clock (cbpi.api.clock), as the rest timer does: on a
simulator at SIM_TIME_SCALE 30 a timer runs 30 times faster. The screens count
down between messages with `scale`, and correct themselves on each message.

Kept in memory only, as the rest timer is: after a restart there are none. The
rest timer (RestTimerController, /resttimer) stays for the brew-day screen as
first released.
"""

import asyncio
import logging
import math

from cbpi.api import clock
from cbpi.api.dataclasses import NotificationAction, NotificationType


class TimerRefused(Exception):
    """A command the timer's state doesn't allow: a fifth timer, starting one
    whose alarm waits, changing the time of one that is counting down."""


def fmt(seconds) -> str:
    """15:00, or 1:30:00 from an hour: as the screen writes a timer's time."""
    s = max(0, int(round(seconds)))
    if s >= 3600:
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


class Timer:
    def __init__(self, ident: int, name: str, seconds: float):
        self.id = ident
        self.name = name
        self.set = float(seconds)  # its own time
        self.run = float(seconds)  # this countdown's length: its own time, or five minutes after +5 MIN
        self.remaining = float(seconds)  # the time left while it isn't running
        self.state = "stopped"
        self.deadline = None  # clock.monotonic() at zero, while it runs
        self.at = None  # clock.now() when it ran out
        self.prompt = None  # its kept prompt's id, while the alarm waits
        self.task = None

    def left(self) -> float:
        if self.state == "running" and self.deadline is not None:
            return max(0.0, self.deadline - clock.monotonic())
        return max(0.0, self.remaining)

    def to_dict(self):
        return dict(id=self.id, name=self.name, set=self.set, run=round(self.run, 3), left=round(self.left(), 3),
                    state=self.state, at=None if self.at is None else round(self.at, 3), prompt=self.prompt)


class TimersController:
    #: The websocket topic every screen hears: data is get_state().
    TOPIC = "timers"
    #: Four at most (the approved design: four in view at once, as a four-channel kitchen timer).
    MAX = 4
    #: A timer's own time: 1 to 300 minutes, the editor's limits.
    MIN_SET = 60
    MAX_SET = 300 * 60
    #: Taking time off never leaves less than 10 seconds; adding never more than a day.
    FLOOR = 10
    CEILING = 24 * 60 * 60
    #: +5 MIN, the alarm's lesser answer.
    MORE = 5 * 60
    #: The alarm's two buttons, as its prompt labels them and as an answer from a timer's row is sent with
    #: the prompt taken down, so a screen that didn't press it can say which.
    MORE_LABEL = "+5 MIN"
    OK_LABEL = "OK"
    #: A new timer's time when the command gives none: the editor's starting 10 minutes.
    DEFAULT_SET = 10 * 60
    NAME_LEN = 24
    NAMES_SETTING = "BREWDAY_TIMER_NAMES"
    NAMES_DEFAULT = "Hop stand, Yeast, Vorlauf"

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.timers = []  # in the order they were added
        self._next_id = 1

    # -- what every screen is told --------------------------------------------------------

    def names(self):
        """The names the screen offers for a timer, from the BREWDAY_TIMER_NAMES setting.

        A list separated by commas; blank gives none. Each is trimmed and kept to
        a name's length; repeats are dropped. Read as the setting is kept, not with
        config.get(), which takes a blank setting for a missing one: Scott's blank
        list would offer the usual names again."""
        try:
            entry = self.cbpi.config.cache.get(self.NAMES_SETTING)
        except Exception:  # noqa: BLE001 - settings that can't be read offer the usual names
            entry = None
        raw = self.NAMES_DEFAULT if entry is None or entry.value is None else entry.value
        names = []
        for part in str(raw).split(","):
            name = " ".join(part.split())[: self.NAME_LEN].strip()
            if name and name.casefold() not in (n.casefold() for n in names):
                names.append(name)
        return names

    def get_state(self):
        return dict(timers=[t.to_dict() for t in self.timers], max=self.MAX, names=self.names(),
                    now=round(clock.now(), 3), scale=clock.scale())

    def _push(self):
        try:
            self.cbpi.ws.send(dict(topic=self.TOPIC, data=self.get_state()))
        except Exception as e:  # noqa: BLE001 - a screen that misses this hears the next one
            self.logger.debug("timers: could not tell the screens: %s", e)

    # -- checking what a command asks for ---------------------------------------------------

    def _find(self, ident) -> Timer:
        try:
            ident = int(ident)
        except (TypeError, ValueError):
            raise LookupError(f"There is no timer {ident!r}.")
        for t in self.timers:
            if t.id == ident:
                return t
        raise LookupError(f"There is no timer {ident}.")

    @staticmethod
    def _number(value, what="seconds") -> float:
        if isinstance(value, bool):
            raise ValueError(f"{what} must be a number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{what} must be a number")
        if not math.isfinite(value):
            raise ValueError(f"{what} must be a number")
        return value

    def _own_time(self, value) -> float:
        seconds = self._number(value)
        if not self.MIN_SET <= seconds <= self.MAX_SET:
            raise ValueError(f"A timer's time is 1 to {self.MAX_SET // 60} minutes.")
        return seconds

    def _name(self, value, fallback: str, t: Timer = None) -> str:
        if value is None:
            return fallback
        if not isinstance(value, str):
            raise ValueError("A timer's name must be text.")
        name = " ".join(value.split())
        if not name:
            return fallback
        if len(name) > self.NAME_LEN:
            raise ValueError(f"A timer's name is {self.NAME_LEN} characters at most.")
        twin = self._twin(name, t)
        if twin is not None:
            raise TimerRefused(f"Another timer is already called {twin.name}. Give this one another name, "
                               "so you can tell their alarms apart.")
        return name

    def _twin(self, name: str, t: Timer = None):
        """Another timer of this name, whatever its capitals: two alarms titled alike are hard to tell apart."""
        key = name.casefold()
        return next((z for z in self.timers if z is not t and z.name.casefold() == key), None)

    def _default_name(self) -> str:
        n = len(self.timers) + 1
        while self._twin(f"Timer {n}") is not None:
            n += 1
        return f"Timer {n}"

    # -- running ------------------------------------------------------------------------------

    def _cancel(self, t: Timer):
        if t.task is not None and not t.task.done():
            t.task.cancel()
        t.task = None

    def _start(self, t: Timer):
        t.deadline = clock.monotonic() + t.remaining
        t.state = "running"
        self._cancel(t)
        t.task = asyncio.get_running_loop().create_task(self._run(t))

    def _answer(self, t: Timer, reason: str, answer=None):
        """Take its alarm's prompt down on every screen. answer: the button it was answered with, when it was."""
        if t.prompt is not None:
            try:
                given = {} if answer is None else {"answer": answer}
                self.cbpi.notification.retire(t.prompt, reason=reason, **given)
            except Exception as e:  # noqa: BLE001
                self.logger.debug("timers: could not take %s's prompt down: %s", t.name, e)
            t.prompt = None
        t.at = None

    async def _run(self, t: Timer):
        try:
            scale = clock.scale()
            while t.state == "running" and t.deadline is not None and clock.monotonic() < t.deadline:
                # In slices of about a real second, as step timers and the rest timer wait: one long sleep
                # keeps the speed the clock had when it began, and a simulator sets SIM_TIME_SCALE a moment
                # after start-up.
                await clock.sleep_until(min(t.deadline, clock.monotonic() + max(1.0, clock.scale())))
                if clock.scale() != scale:
                    scale = clock.scale()
                    self._push()  # the screens count down at the new speed
            if t.state == "running" and t in self.timers:
                self._finish(t)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a timer that dies says so instead of sitting at zero forever
            self.logger.error("timer %s stopped: %s", t.name, e)
            t.remaining = t.left()
            t.state = "paused"
            t.deadline = None
            self._push()

    def _finish(self, t: Timer):
        t.state = "done"
        t.remaining = 0.0
        t.deadline = None
        t.task = None
        t.at = clock.now()
        ident = t.id

        async def more():
            await self.plus5(ident)

        async def ok():
            await self.ok(ident)

        t.prompt = self.cbpi.notify(
            t.name,
            f"The {fmt(t.set)} timer has run out.",
            NotificationType.WARNING,
            action=[NotificationAction(self.MORE_LABEL, more), NotificationAction(self.OK_LABEL, ok)],
            keep=True,
        )
        self._push()

    # -- the commands -------------------------------------------------------------------------

    async def add(self, name=None, seconds=None, start=False):
        """A new timer at its own time, stopped, or counting down at once (start)."""
        if len(self.timers) >= self.MAX:
            raise TimerRefused("Four timers at most. Remove one to add another.")
        seconds = self._own_time(self.DEFAULT_SET if seconds is None else seconds)
        t = Timer(self._next_id, self._name(name, self._default_name()), seconds)
        self._next_id += 1
        self.timers.append(t)
        if start:
            self._start(t)
        self._push()
        return self.get_state()

    async def change(self, ident, name=None, seconds=None):
        """Rename a timer, or set a stopped one's own time. A timer that is counting down
        or paused changes its time left with add_time; one whose alarm waits is answered
        by a new time and stopped at it."""
        t = self._find(ident)
        new_name = self._name(name, t.name, t)
        if seconds is not None:
            seconds = self._own_time(seconds)
            if t.state in ("running", "paused"):
                raise TimerRefused(f"{t.name} is counting down: change its time left instead.")
            self._answer(t, "withdrawn")
            t.set = t.run = t.remaining = seconds
            t.state = "stopped"
        t.name = new_name
        self._push()
        return self.get_state()

    async def start(self, ident):
        """Count down: a stopped timer from its own time, a paused one from where it is."""
        t = self._find(ident)
        if t.state == "running":
            return self.get_state()
        if t.state == "done":
            raise TimerRefused(f"{t.name} has run out: answer it with OK or +5 MIN.")
        if t.state == "stopped":
            t.run = t.remaining
        self._start(t)
        self._push()
        return self.get_state()

    async def pause(self, ident):
        t = self._find(ident)
        if t.state != "running":
            return self.get_state()
        t.remaining = t.left()
        t.state = "paused"
        t.deadline = None
        self._cancel(t)
        self._push()
        return self.get_state()

    async def add_time(self, ident, seconds):
        """A minute or five more or less on a timer that is counting down or paused, at once.
        Never less than 10 seconds left; the countdown's length grows with it."""
        t = self._find(ident)
        seconds = self._number(seconds)
        if t.state not in ("running", "paused"):
            raise TimerRefused(f"{t.name} isn't counting down: set its time instead.")
        was = t.left()
        left = min(float(self.CEILING), max(float(self.FLOOR), was + seconds))
        t.run = max(left, t.run + left - was)
        if t.state == "running":
            # sleeping toward the old zero would end a shortened timer late
            t.remaining = left
            self._start(t)
        else:
            t.remaining = left
        self._push()
        return self.get_state()

    async def remove(self, ident):
        t = self._find(ident)
        self._cancel(t)
        self._answer(t, "withdrawn")
        t.state = "removed"
        self.timers.remove(t)
        self._push()
        return self.get_state()

    async def ok(self, ident):
        """The alarm answered: back to its own time, stopped, ready to start again."""
        t = self._find(ident)
        if t.state != "done":
            return self.get_state()
        self._answer(t, "answered", self.OK_LABEL)
        t.state = "stopped"
        t.remaining = t.run = t.set
        self._push()
        return self.get_state()

    async def plus5(self, ident):
        """The alarm answered with five minutes more: it counts down again from 5:00."""
        t = self._find(ident)
        if t.state != "done":
            return self.get_state()
        self._answer(t, "answered", self.MORE_LABEL)
        t.remaining = t.run = float(self.MORE)
        self._start(t)
        self._push()
        return self.get_state()
