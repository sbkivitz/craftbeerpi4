"""The brew-day screen's timers: up to four, kept here so every screen shows the same ones.

Lots of things in a brew day want timing, not only a rest: several timers run
side by side, each can be named, and one that runs out sounds an alarm and
shows a visual alert.

A timer has a name ("Timer 1" until it is given one), its own time, and a
state: stopped (ready, at its own time), running, paused, or done (it ran out,
and its alarm waits for an answer). At zero it raises a prompt titled with its
name, with +5 MIN and OK, which every screen keeps until somebody answers it on
any one of them. OK puts the timer back to its own time, stopped, ready to start
again, as a kitchen timer's recall does; +5 MIN runs it for five minutes more.

The screen offers names for a timer from the BREWDAY_TIMER_NAMES setting, a list
separated by commas.

They run on the brewing clock (cbpi.api.clock), as the rest timer does: on a
simulator at SIM_TIME_SCALE 30 a timer runs 30 times faster. The screens count
down between messages with `scale`, and correct themselves on each message.

They are kept on disk too, in timers.json in the config folder, so a restart -
a power cut, say - doesn't lose them. The file is replaced whole (written
beside it, then renamed over it) on every change, and every SAVE_EVERY brewing
seconds while a timer counts down. When CraftBeerPi starts again (init):
- a timer that was counting down comes back paused, with the time it had left
  at the last save, and waits for somebody to start it: nothing tells
  CraftBeerPi whether the brew went on while it was off;
- one that had run out comes back run out, its prompt raised again for every
  screen, until it is answered;
- a paused or stopped one comes back as it was.
Each timer that was counting down or had run out says how long CraftBeerPi was
off (`off`, in real seconds), until it is started or answered. That length is
measured on the real clock, which on a Raspberry Pi without a clock board
starts wrong after a power cut, until the network sets it. So it is given only
when the system says its clock is synchronized now and said so at the last
save, and only when a timer was counting down then - otherwise the last save
can be long before CraftBeerPi stopped. Otherwise `off` is null: not known.
How long ago a timer brought back run out ran out is known when the clock was
synchronized at the last save and is now; otherwise its `at` is null.

Whether the clock is synchronized comes from timedatectl, by the rule a step
carrying on after a restart uses too (cbpi.api.system_clock): wherever it
exists - Linux, and so a Raspberry Pi - only its plain "yes" counts. "no", a
failure, a timeout or an answer it doesn't give all count as not synchronized,
and the log says what went wrong, so a user not allowed to run it leaves every
length not known rather than worked out from a wrong clock. Only a system with
no timedatectl at all - Windows, as on the simulators - counts as synchronized:
its battery-backed clock keeps time through a power cut.

A missing or damaged file, or one of another version, brings back no timers,
and the log says why. One that can't be used is kept beside it, and an empty
one written in its place.

The rest timer (RestTimerController, /resttimer) stays alongside, in memory
only, for brew-day screens that use it.
"""

import asyncio
import json
import logging
import math
import os

from cbpi.api import clock
from cbpi.api.dataclasses import NotificationAction, NotificationType
from cbpi.api.persist import atomic_write_json, quarantine
from cbpi.api.system_clock import system_clock_trusted

# Real time, apart from the brewing clock: when the file was saved, and so how long CraftBeerPi was off.
_REAL = clock.RealTimeSource()


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
        self.at = None  # clock.now() when it ran out, while that is known
        self.rang = None  # clock.monotonic() when it ran out, while that is known
        self.prompt = None  # its kept prompt's id, while the alarm waits
        self.task = None
        # Counting down or run out when CraftBeerPi stopped, until it is started or answered; and how long
        # CraftBeerPi was off then, in real seconds, when that is known.
        self.interrupted = False
        self.off = None

    def left(self) -> float:
        if self.state == "running" and self.deadline is not None:
            return max(0.0, self.deadline - clock.monotonic())
        return max(0.0, self.remaining)

    def up(self):
        """Brewing seconds since it ran out, or None: it hasn't, or how long ago isn't known."""
        if self.state != "done" or self.rang is None:
            return None
        return max(0.0, clock.monotonic() - self.rang)

    def to_dict(self):
        return dict(id=self.id, name=self.name, set=self.set, run=round(self.run, 3), left=round(self.left(), 3),
                    state=self.state, at=None if self.at is None else round(self.at, 3), prompt=self.prompt,
                    interrupted=self.interrupted, off=None if self.off is None else round(self.off, 1))


class TimersController:
    #: The websocket topic every screen hears: data is get_state().
    TOPIC = "timers"
    #: Four at most: four in view at once, as on a four-channel kitchen timer.
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
    #: The file in the config folder the timers are kept in, and the version of its form this reads.
    FILE = "timers.json"
    FILE_VERSION = 1
    #: While a timer counts down, the file is saved this often as well, in brewing seconds: a power cut
    #: costs a timer at most this much of its countdown - it comes back with up to this much too long -
    #: and makes the time CraftBeerPi was off at most this much too long. At real speed, six small writes
    #: a minute on the Pi's SD card, and none while nothing counts down. Brewing seconds, so a simulation
    #: loses the same at any speed.
    SAVE_EVERY = 10
    #: Real seconds between asking whether the system clock is synchronized, while a timer counts down or
    #: a restart's numbers wait for the answer.
    CLOCK_POLL = 10
    #: Whether the system clock can be trusted to say how long CraftBeerPi was off (cbpi.api.system_clock):
    #: (trusted, what went wrong). Run in a thread: it may start a program.
    clock_trusted = staticmethod(system_clock_trusted)

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.timers = []  # in the order they were added
        self._next_id = 1
        folder = getattr(cbpi, "config_folder", None)
        self.path = folder.get_file_path(self.FILE) if folder is not None else None
        self._keeper = None  # _keep, while a timer counts down or has run out
        self._watch = None  # asking whether the system clock is synchronized
        # True once the system clock can be trusted (system_clock_trusted), False while it can't, None before it
        # has been asked; and what has gone wrong asking it, each said once in the log.
        self._trusted = None
        self._clock_problems = set()
        # What a restart left to work out once the clock can be trusted (_restore, _settle), or None.
        self._restart = None
        self._save_failed = False

    # -- what every screen is told --------------------------------------------------------

    def names(self):
        """The names the screen offers for a timer, from the BREWDAY_TIMER_NAMES setting.

        A list separated by commas; blank gives none. Each is trimmed and kept to
        a name's length; repeats are dropped. Read as the setting is kept, not with
        config.get(), which takes a blank setting for a missing one: a blank list
        would offer the usual names again."""
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

    def _changed(self):
        """A timer changed: saved, then every screen told."""
        self._save()
        self._push()

    # -- kept on disk ---------------------------------------------------------------------------

    STATES = ("stopped", "running", "paused", "done")

    def _running(self) -> bool:
        return any(t.state == "running" for t in self.timers)

    def _ringing(self) -> bool:
        return any(t.state == "done" for t in self.timers)

    def _file_state(self):
        """What timers.json holds: each timer as it stands, and when that was on the real clock."""
        timers = []
        for t in self.timers:
            up = t.up()
            timers.append(dict(id=t.id, name=t.name, set=t.set, run=round(t.run, 3), left=round(t.left(), 3),
                               state=t.state, up=None if up is None else round(up, 3), interrupted=t.interrupted,
                               off=None if t.off is None else round(t.off, 3)))
        return dict(version=self.FILE_VERSION, saved_at=_REAL.now(), clock_confirmed=self._trusted is True,
                    scale=clock.scale(), next_id=self._next_id, timers=timers)

    def _save(self):
        """Replace timers.json with the timers as they are now. Never raises: a disk that fails costs a restart
        its timers, not the brew its alarms."""
        if self.path is None:
            return
        try:
            atomic_write_json(self.path, self._file_state(), sort_keys=True)
        except Exception as e:  # noqa: BLE001
            if not self._save_failed:
                self.logger.warning("Timers: could not save %s, so a restart now would lose them: %s", self.path, e)
            self._save_failed = True
            return
        if self._save_failed:
            self.logger.info("Timers: %s saved again", self.path)
        self._save_failed = False

    def _start_keeper(self):
        if self._keeper is None or self._keeper.done():
            self._keeper = asyncio.get_running_loop().create_task(self._keep())

    async def _keep(self):
        """While a timer counts down, save every SAVE_EVERY brewing seconds as well: the time it has left, and
        that CraftBeerPi was still running then. While one counts down or has run out, tell the screens when the
        clock's speed changes: they count between messages at the speed they were last told, and a simulator
        sets its speed a moment after start-up, when a timer brought back may already count up."""
        try:
            scale = clock.scale()
            due = clock.monotonic() + self.SAVE_EVERY
            while self._running() or self._ringing():
                # In slices of about a real second, as the timers wait, so a new speed is followed at once.
                await clock.sleep_until(min(due, clock.monotonic() + max(1.0, clock.scale())))
                if clock.scale() != scale:
                    scale = clock.scale()
                    self._push()
                if clock.monotonic() >= due:
                    if self._running():
                        self._save()
                    due = clock.monotonic() + self.SAVE_EVERY
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self.logger.error("Timers: saving them while they count down stopped: %s", e)

    def _watch_clock(self):
        if self._trusted is not True and (self._watch is None or self._watch.done()):
            self._watch = asyncio.get_running_loop().create_task(self._until_trusted())

    async def _until_trusted(self):
        """Ask whether the system clock can be trusted, and again every CLOCK_POLL real seconds while a timer counts
        down or a restart's numbers wait for it. Once it can, work those numbers out."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                trusted, problem = await loop.run_in_executor(None, self.clock_trusted)
                if trusted:
                    self._trusted = True
                    if self._restart_waits():
                        self._settle()
                        self._changed()
                    return
                self._trusted = False
                if problem and problem not in self._clock_problems:
                    self._clock_problems.add(problem)
                    self.logger.warning("Timers: %s, so the system clock counts as not synchronized: how long "
                                        "CraftBeerPi was off isn't known until timedatectl says yes.", problem)
                if not self._restart_waits() and not self._running():
                    return
                await _REAL.sleep(self.CLOCK_POLL)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - unanswered, the numbers stay unknown, which is what they are
            self.logger.warning("Timers: could not ask whether the clock is synchronized: %s", e)

    def _restart_waits(self) -> bool:
        """Whether a timer brought back still waits for the clock, to say how long CraftBeerPi was off."""
        r = self._restart
        if r is not None and not any(t in self.timers and t.interrupted for t, _, _ in r["timers"]):
            self._restart = None
        return self._restart is not None

    async def init(self):
        """At start-up: bring back the timers saved before CraftBeerPi stopped (see the module's docstring),
        and start asking whether the system clock can be trusted."""
        app = getattr(self.cbpi, "app", None)
        if app is not None:
            app.on_cleanup.append(self.shutdown)
        if self._restore():
            self._changed()
            self._start_keeper()
        elif self.path is not None and not os.path.exists(self.path):
            # None to read, or one that couldn't be used and was moved aside: an empty file, so the next start
            # has one and says nothing.
            self._save()
        self._watch_clock()

    async def shutdown(self, app=None):
        """On a stop: a timer counting down is saved with the time it has left now."""
        for task in (self._keeper, self._watch):
            if task is not None and not task.done():
                task.cancel()
        if self._running():
            self._save()

    def _restore(self) -> bool:
        """Bring back the timers in timers.json. True if there were any."""
        saved = self._read()
        if not saved:
            return False
        self._next_id = saved["next_id"]
        if not saved["timers"]:
            return False
        live = any(d["state"] == "running" for d in saved["timers"])
        # Each timer whose numbers wait for the clock: whether this restart's length is its own to show, and the
        # brewing seconds it had been run out at the save.
        waiting = []
        said = []
        for d in saved["timers"]:
            t = Timer(d["id"], d["name"], d["set"])
            t.run, t.remaining, t.state = d["run"], d["left"], d["state"]
            t.interrupted, t.off = d["interrupted"], d["off"]
            if t.state == "running":
                t.state, t.interrupted, t.off = "paused", True, None
                waiting.append((t, True, None))
                said.append(f"{t.name} paused at {fmt(t.remaining)} (it was counting down)")
            elif t.state == "done":
                t.remaining = 0.0
                own = not t.interrupted
                if own:
                    t.interrupted, t.off = True, None
                waiting.append((t, own, d["up"]))
                said.append(f"{t.name} run out, its alarm raised again")
            else:
                said.append(f"{t.name} {t.state} at {fmt(t.remaining)}")
            self.timers.append(t)
        for t in self.timers:
            if t.state == "done":
                self._ring(t)
        if waiting and saved["clock_confirmed"]:
            self._restart = dict(saved_at=saved["saved_at"], scale=saved["scale"], live=live, timers=waiting,
                                 real=_REAL.monotonic(), brewing=clock.monotonic())
        self.logger.info("Timers: brought back %d from %s: %s.%s", len(self.timers), os.path.basename(self.path),
                         "; ".join(said), "" if not waiting or saved["clock_confirmed"] else
                         " The system clock wasn't known to be synchronized when they were saved, so how long"
                         " CraftBeerPi was off isn't known.")
        return True

    def _settle(self):
        """A restart's numbers, once the clock can be trusted: how long CraftBeerPi was off, and how long ago each
        timer brought back run out ran out."""
        r, self._restart = self._restart, None
        if r is None:
            return
        # When the timers were brought back, by the real clock as it is now, set right.
        back = _REAL.now() - (_REAL.monotonic() - r["real"])
        since = back - r["saved_at"]  # real seconds from the last save until then
        if since < 0:
            self.logger.warning("Timers: by the clock they were saved after they were brought back, so how long "
                                "CraftBeerPi was off isn't known.")
            return
        for t, own, up in r["timers"]:
            if t not in self.timers or not t.interrupted:
                continue  # removed, started or answered meanwhile
            if own and r["live"]:
                t.off = since
            if t.state == "done" and up is not None:
                # The time off passes at the speed the clock had, as a simulated brewery cools at it.
                t.rang = r["brewing"] - (up + since * r["scale"])
                t.at = clock.now() - (clock.monotonic() - t.rang)
        if r["live"]:
            self.logger.info("Timers: CraftBeerPi was off for %.0f s before they were brought back.", since)

    def _read(self):
        """timers.json, checked (_check), or None, with a line in the log saying why."""
        if self.path is None:
            return None
        name = os.path.basename(self.path)
        try:
            with open(self.path, encoding="utf-8") as handle:
                text = handle.read()
        except FileNotFoundError:
            self.logger.warning("Timers: none brought back: there was no %s. An empty one is written now.", name)
            return None
        except (OSError, ValueError) as e:
            why = e
        else:
            try:
                return self._check(json.loads(text))
            except (ValueError, TypeError) as e:
                why = e
        kept = quarantine(self.path)
        self.logger.warning("Timers: none brought back: %s can't be used (%s). It was kept as %s.", name, why,
                            os.path.basename(kept) if kept else "nothing: it couldn't be moved")
        return None

    def _check(self, data):
        """The file's contents, if they are what _file_state writes; else ValueError, saying what isn't."""
        if not isinstance(data, dict):
            raise ValueError("it isn't a JSON object")
        if data.get("version") != self.FILE_VERSION:
            raise ValueError(f"it is version {data.get('version')!r}; this CraftBeerPi reads {self.FILE_VERSION}")

        def number(d, key, low, high, empty=False):
            value = d.get(key)
            if value is None and empty:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not low <= value <= high:
                raise ValueError(f"{key} is {value!r}")
            return float(value)

        def flag(d, key):
            if not isinstance(d.get(key), bool):
                raise ValueError(f"{key} is {d.get(key)!r}")
            return d[key]

        next_id = data.get("next_id")
        if isinstance(next_id, bool) or not isinstance(next_id, int) or next_id < 1:
            raise ValueError(f"next_id is {next_id!r}")
        saved = dict(saved_at=number(data, "saved_at", 0, math.inf), clock_confirmed=flag(data, "clock_confirmed"),
                     scale=number(data, "scale", 0, math.inf), next_id=next_id, timers=[])
        if saved["scale"] <= 0:
            raise ValueError("scale is 0")
        timers = data.get("timers")
        if not isinstance(timers, list) or len(timers) > self.MAX:
            raise ValueError(f"it holds {timers!r:.40} for its timers")
        names = set()
        for d in timers:
            if not isinstance(d, dict):
                raise ValueError(f"a timer is {d!r:.40}")
            ident, name = d.get("id"), d.get("name")
            if isinstance(ident, bool) or not isinstance(ident, int) or not 1 <= ident < next_id \
                    or ident in (x["id"] for x in saved["timers"]):
                raise ValueError(f"a timer's id is {ident!r}")
            if not isinstance(name, str) or not name or name != " ".join(name.split()) \
                    or len(name) > self.NAME_LEN or name.casefold() in names:
                raise ValueError(f"a timer's name is {name!r}")
            names.add(name.casefold())
            if d.get("state") not in self.STATES:
                raise ValueError(f"{name}'s state is {d.get('state')!r}")
            saved["timers"].append(dict(
                id=ident, name=name, state=d["state"],
                set=number(d, "set", self.MIN_SET, self.MAX_SET), run=number(d, "run", 0, self.CEILING),
                left=number(d, "left", 0, self.CEILING), up=number(d, "up", 0, math.inf, empty=True),
                interrupted=flag(d, "interrupted"), off=number(d, "off", 0, math.inf, empty=True)))
        return saved

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
        t.interrupted, t.off = False, None
        self._cancel(t)
        t.task = asyncio.get_running_loop().create_task(self._run(t))
        self._start_keeper()
        self._watch_clock()

    def _answer(self, t: Timer, reason: str, answer=None):
        """Take its alarm's prompt down on every screen. answer: the button it was answered with, when it was."""
        if t.prompt is not None:
            try:
                given = {} if answer is None else {"answer": answer}
                self.cbpi.notification.retire(t.prompt, reason=reason, **given)
            except Exception as e:  # noqa: BLE001
                self.logger.debug("timers: could not take %s's prompt down: %s", t.name, e)
            t.prompt = None
        t.at = t.rang = None
        t.interrupted, t.off = False, None

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
            self._changed()

    def _finish(self, t: Timer):
        t.state = "done"
        t.remaining = 0.0
        t.deadline = None
        t.task = None
        t.at, t.rang = clock.now(), clock.monotonic()
        self._ring(t)
        self._changed()

    def _ring(self, t: Timer):
        """Its alarm: a prompt titled with its name, with +5 MIN and OK, kept for every screen until answered."""
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
        self._changed()
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
        self._changed()
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
        self._changed()
        return self.get_state()

    async def pause(self, ident):
        t = self._find(ident)
        if t.state != "running":
            return self.get_state()
        t.remaining = t.left()
        t.state = "paused"
        t.deadline = None
        self._cancel(t)
        self._changed()
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
        self._changed()
        return self.get_state()

    async def remove(self, ident):
        t = self._find(ident)
        self._cancel(t)
        self._answer(t, "withdrawn")
        t.state = "removed"
        self.timers.remove(t)
        self._changed()
        return self.get_state()

    async def ok(self, ident):
        """The alarm answered: back to its own time, stopped, ready to start again."""
        t = self._find(ident)
        if t.state != "done":
            return self.get_state()
        self._answer(t, "answered", self.OK_LABEL)
        t.state = "stopped"
        t.remaining = t.run = t.set
        self._changed()
        return self.get_state()

    async def plus5(self, ident):
        """The alarm answered with five minutes more: it counts down again from 5:00."""
        t = self._find(ident)
        if t.state != "done":
            return self.get_state()
        self._answer(t, "answered", self.MORE_LABEL)
        t.remaining = t.run = float(self.MORE)
        self._start(t)
        self._changed()
        return self.get_state()
