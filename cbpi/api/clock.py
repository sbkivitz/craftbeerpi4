"""One clock for everything that measures time or waits for a duration.

A simulated rig is only useful if a whole brew day can be rehearsed in minutes,
and only trustworthy if doing so gives the same answer as running it in real
time. Those two requirements together mean *time itself* is what scales - not
individual call sites.

Scaling each sleep and duration where it appears is a trap.
Timing lives in a lot of places: the step Timer, every kettle logic's control
loop, pump duty cycles, sensor loops, PID sample gates. Miss one and that part
keeps running at real speed, which does not merely slow the run down - it changes
the answer. A control loop sampling sixty times slower relative to its plant is a
different closed-loop system, and it limit cycles where the real one holds steady.
A mash that holds 152 F in real time swings about 10 F when only the thermal
model is scaled.

So everything that asks the time, or waits, comes through here:

    now()             simulated wall-clock seconds, like time.time()
    monotonic()       simulated monotonic seconds, for measuring intervals
    await sleep(n)    wait n SIMULATED seconds
    await sleep_until(t)  wait until simulated time reaches t

Prefer `sleep_until` whenever the thing being waited for is an instant rather
than a duration - the end of a mash rest, the next hop addition. Repeatedly
sleeping a fixed step and re-checking looks equivalent and is not: the last step
always lands somewhere past the deadline, and under a scaled clock that
overshoot is multiplied by the scale. A deadline is also the only form a virtual
clock can honour exactly, because it says where to jump to.

On real hardware the default source is real time, `now()` is `time.time()` and
`sleep()` is `asyncio.sleep()`. Nothing changes, and no caller needs to know
whether it is being simulated.

Only a simulator changes the speed. `set_scale()` keeps simulated time where it
is; `reset()` takes now() back to the computer's date and time and runs at the
new speed from there, leaving monotonic() unbroken. Whatever holds a time on
now() has to move with it, which is CraftBeerPi.brewing_clock's job
(cbpi.controller.clock_controller).

## Extending

The source is pluggable. Implement `TimeSource` and install it:

    clock.set_source(MyTimeSource())

Two are provided. `RealTimeSource` is the default. `ScaledTimeSource` runs a fixed
number of simulated seconds per real second, which is what the SimVessel testbench
uses. `VirtualTimeSource` does not wait at all: it steps straight to the next
scheduled wake, so a sixty minute rest costs microseconds and lands exactly on
its deadline instead of within a scheduler quantum of it. That makes it the right
clock for tests about *durations*; the scaled source remains the one for tests
about a running server.
"""

import abc
import asyncio
import heapq
import math
import time

__all__ = [
    "TimeSource",
    "RealTimeSource",
    "ScaledTimeSource",
    "VirtualTimeSource",
    "now",
    "monotonic",
    "sleep",
    "sleep_until",
    "scale",
    "set_scale",
    "reset",
    "is_scaled",
    "get_source",
    "set_source",
]


class TimeSource(abc.ABC):
    """Where time comes from. Implement to change how a simulation advances."""

    #: Simulated seconds per real second. Informational; a source that does not
    #: advance at a fixed rate should report 1.0.
    rate = 1.0

    @abc.abstractmethod
    def now(self) -> float:
        """Current simulated time in seconds, in the manner of time.time()."""

    @abc.abstractmethod
    def monotonic(self) -> float:
        """Monotonic simulated seconds. Never goes backwards."""

    @abc.abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Wait `seconds` of simulated time."""

    async def sleep_until(self, deadline: float) -> None:
        """Wait until simulated time reaches `deadline`.

        The default derives the remaining duration from the absolute deadline on
        every call, so waking late does not shorten the next wait or push the
        target out. Sources that can do better - a virtual clock, which simply
        moves to the deadline - override this.
        """
        await self.sleep(max(0.0, deadline - self.now()))


class RealTimeSource(TimeSource):
    """Real time. The default, and what every physical brewery runs on."""

    rate = 1.0

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class ScaledTimeSource(TimeSource):
    """Simulated time running at a fixed multiple of real time.

    Anchored rather than computed from an epoch, so the rate can change while the
    server is running without simulated time jumping. A jump would make every
    timer already counting down appear to have expired.
    """

    #: Below this, asyncio cannot usefully resolve a sleep and the loop starts to
    #: spin rather than wait. Sleeps are floored here, so an over-ambitious scale
    #: makes the simulation run slower than asked rather than incorrectly.
    MIN_SLEEP = 0.001

    def __init__(self, rate: float, reference: TimeSource = None):
        self._reference = reference or RealTimeSource()
        self._rate = self._validate(rate)
        self._real_anchor = self._reference.now()
        self._sim_anchor = self._real_anchor
        self._real_mono_anchor = self._reference.monotonic()
        self._sim_mono_anchor = self._real_mono_anchor

    @staticmethod
    def _validate(rate) -> float:
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            raise ValueError("time scale must be a number, got {!r}".format(rate))
        # Positive is not enough: float("inf") and float("nan") both get past a
        # bare `rate <= 0`, and SIM_TIME_SCALE is a free-text number setting.
        #
        # An infinite rate makes every elapsed interval infinite, which is not a
        # fast simulation but a hang - a consumer integrating over that interval
        # never terminates, taking the whole event loop with it and leaving
        # every GPIO pin latched wherever it was. A NaN rate is worse: it
        # poisons every subsequent time arithmetic silently, and NaN comparisons
        # are all False so no deadline is ever reached.
        if not math.isfinite(rate):
            raise ValueError("time scale must be finite, got {}".format(rate))
        if rate <= 0:
            raise ValueError("time scale must be positive, got {}".format(rate))
        return rate

    @property
    def rate(self) -> float:
        return self._rate

    @rate.setter
    def rate(self, value) -> None:
        value = self._validate(value)
        # Re-anchor at the current simulated instant before changing the rate, so
        # the clock carries on from where it is rather than leaping.
        self._sim_anchor = self.now()
        self._real_anchor = self._reference.now()
        self._sim_mono_anchor = self.monotonic()
        self._real_mono_anchor = self._reference.monotonic()
        self._rate = value

    def reset(self, rate) -> float:
        """Back to the reference clock's date and time, running at `rate` from there.

        Unlike setting `rate`, which carries on from where simulated time is,
        this undoes what an accelerated run did to the date: now() is the
        reference's (the computer's) again. monotonic() carries on from where it
        is, as a monotonic clock must, so whatever measures an interval on it
        goes on unbroken. Returns how far now() moved, in seconds: negative
        when it went back. Whatever holds a time on now() has to move it by as
        much (CraftBeerPi.brewing_clock does that for the server).
        """
        rate = self._validate(rate)
        before = self.now()
        self._sim_mono_anchor = self.monotonic()
        self._real_mono_anchor = self._reference.monotonic()
        self._real_anchor = self._reference.now()
        self._sim_anchor = self._real_anchor
        self._rate = rate
        return self._sim_anchor - before

    def now(self) -> float:
        elapsed = self._reference.now() - self._real_anchor
        return self._sim_anchor + elapsed * self._rate

    def monotonic(self) -> float:
        elapsed = self._reference.monotonic() - self._real_mono_anchor
        return self._sim_mono_anchor + elapsed * self._rate

    async def sleep(self, seconds: float) -> None:
        try:
            real = float(seconds) / self._rate
        except (TypeError, ValueError):
            real = 0.0
        if real <= 0:
            # Still yield, so a caller looping on sleep(0) cannot starve the loop.
            await asyncio.sleep(0)
            return
        await asyncio.sleep(max(self.MIN_SLEEP, real))


class VirtualTimeSource(TimeSource):
    """Time that only moves when nothing else can run.

    Nothing ever really waits. Every sleeper registers the instant it wants to
    wake at; when the event loop has no more work, the clock jumps to the
    earliest of those instants and wakes whatever is due. A sixty minute rest
    costs microseconds, and - because the jump lands exactly on the deadline - it
    measures exactly sixty minutes rather than sixty minutes plus however long
    the operating system took to notice.

    That exactness is the point. A test of "does a sixty minute rest last sixty
    minutes" run against a scaled real clock can only ever assert a tolerance,
    and that tolerance has to be widened until it passes on a loaded machine, at
    which point it no longer tests much. Here the answer is exact and does not
    depend on what else the machine is doing.

    Deadlines are honoured in order, so tasks stay in the right sequence relative
    to one another. What it does not model is work that takes real time: a
    computation between two sleeps appears instantaneous. For control loops
    reacting to a simulated plant that is fine, because the plant is advanced by
    the same clock.

    now() and monotonic() move together but MONOTONIC_OFFSET apart, as the two
    are apart on a real clock, where now() counts from 1970 and monotonic() from
    an arbitrary start. Were they one number, code that takes a time from one and
    uses it as the other would pass every test here and go wrong on a rig: a
    deadline on monotonic() handed to sleep_until(), which waits for a time on
    now(), is already past there, so the wait returns at once and the loop
    spins. monotonic() is ahead here, so such a wait lasts decades instead, and
    the test fails.

    Drive it with `run()`:

        virtual = VirtualTimeSource()
        clock.set_source(virtual)
        task = asyncio.create_task(something_that_sleeps())
        await virtual.run()
    """

    rate = 1.0

    #: Seconds monotonic() runs ahead of now(): about as far as a real clock's
    #: two are apart.
    MONOTONIC_OFFSET = 1.7e9

    def __init__(self, start: float = 1_000_000.0):
        self._now = float(start)
        self._waiters = []
        self._seq = 0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._now + self.MONOTONIC_OFFSET

    async def sleep(self, seconds: float) -> None:
        try:
            seconds = max(0.0, float(seconds))
        except (TypeError, ValueError):
            seconds = 0.0
        await self.sleep_until(self._now + seconds)

    async def sleep_until(self, deadline: float) -> None:
        if deadline <= self._now:
            # Still yield: a caller looping on an elapsed deadline must not be
            # able to starve the event loop.
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._waiters, (deadline, self._seq, future))
        try:
            await future
        except asyncio.CancelledError:
            # Drop the registration so a cancelled sleeper cannot hold time back.
            self._waiters = [w for w in self._waiters if w[2] is not future]
            heapq.heapify(self._waiters)
            raise

    @property
    def pending(self) -> int:
        """How many sleepers are waiting."""
        return len(self._waiters)

    def _advance(self) -> bool:
        """Jump to the earliest deadline and wake everything due there."""
        if not self._waiters:
            return False
        self._now = max(self._now, self._waiters[0][0])
        while self._waiters and self._waiters[0][0] <= self._now:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)
        return True

    async def run(self, until: float = None, max_steps: int = 1_000_000) -> None:
        """Let simulated time run until nothing is waiting, or until `until`.

        `max_steps` bounds a run that would otherwise never settle - a control
        loop sleeping forever is normal, and without a bound the test would hang
        rather than fail.
        """
        steps = 0
        while steps < max_steps:
            # Let anything just woken get as far as its next wait before
            # deciding where time goes next.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if not self._waiters:
                return
            if until is not None and self._waiters[0][0] > until:
                self._now = until
                return
            self._advance()
            steps += 1


_source: TimeSource = RealTimeSource()


def get_source() -> TimeSource:
    """The time source currently in use."""
    return _source


def set_source(source: TimeSource) -> TimeSource:
    """Install a time source. Returns the one that was replaced."""
    global _source
    if not isinstance(source, TimeSource):
        raise TypeError("expected a TimeSource, got {!r}".format(type(source)))
    previous, _source = _source, source
    return previous


def now() -> float:
    return _source.now()


def monotonic() -> float:
    return _source.monotonic()


async def sleep(seconds: float) -> None:
    await _source.sleep(seconds)


async def sleep_until(deadline: float) -> None:
    """Wait until simulated time reaches `deadline`.

    Use this instead of sleeping a fixed step and re-checking whenever what is
    being waited for is an instant. See the module docstring.
    """
    await _source.sleep_until(deadline)


def scale() -> float:
    """Simulated seconds per real second. 1.0 on real hardware."""
    return _source.rate


def is_scaled() -> bool:
    return _source.rate != 1.0


def set_scale(value) -> float:
    """Convenience: run at `value` simulated seconds per real second.

    Adjusts the current ScaledTimeSource if there is one, so simulated time stays
    continuous; otherwise installs a new one. Returns the rate in effect, which is
    unchanged if `value` is not usable.
    """
    global _source
    try:
        rate = ScaledTimeSource._validate(value)
    except ValueError:
        return _source.rate
    if isinstance(_source, ScaledTimeSource):
        _source.rate = rate
    elif rate != 1.0:
        _source = ScaledTimeSource(rate)
    return _source.rate


def reset(value):
    """Back to the computer's date and time, running at `value` from there.

    For a simulator changing its speed (cbpi4-simvessel's SIM_TIME_SCALE);
    nothing on a rig calls it. set_scale() keeps simulated time where it is, so
    an hour at 30x leaves the clock thirty hours ahead of the computer's; this
    puts it back. monotonic() carries on unbroken (ScaledTimeSource.reset).

    Returns how far now() moved, in seconds (negative: back), or None when
    `value` is not a usable rate, and nothing changes. Whatever holds a time on
    now() must move it by as much: CraftBeerPi.brewing_clock does that for the
    server, and is what a plugin should call rather than this.
    """
    global _source
    try:
        rate = ScaledTimeSource._validate(value)
    except ValueError:
        return None
    if isinstance(_source, ScaledTimeSource):
        return _source.reset(rate)
    if isinstance(_source, RealTimeSource):
        if rate != 1.0:
            # Starts at the computer's time, and its monotonic() at the real one.
            _source = ScaledTimeSource(rate)
        return 0.0
    # Any other source - a virtual clock in a test - gives way to the computer's
    # time at that rate, its monotonic() carrying on from where it was.
    previous = _source
    before, mono = previous.now(), previous.monotonic()
    scaled = ScaledTimeSource(rate)
    scaled._sim_mono_anchor = mono
    _source = scaled
    return scaled.now() - before
