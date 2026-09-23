"""One clock for everything that measures time or waits for a duration.

A simulated rig is only useful if a whole brew day can be rehearsed in minutes,
and only trustworthy if doing so gives the same answer as running it in real
time. Those two requirements together mean *time itself* is what scales - not
individual call sites.

Scaling each sleep and duration where it appears was tried first and is a trap.
Timing lives in a lot of places: the step Timer, every kettle logic's control
loop, pump duty cycles, sensor loops, PID sample gates. Miss one and that part
keeps running at real speed, which does not merely slow the run down - it changes
the answer. A control loop sampling sixty times slower relative to its plant is a
different closed-loop system, and it limit cycles where the real one holds steady.
That was observed: a mash that holds 152 F in real time swung about 10 F when only
the thermal model had been scaled.

So everything that asks the time, or waits, comes through here:

    now()             simulated wall-clock seconds, like time.time()
    monotonic()       simulated monotonic seconds, for measuring intervals
    await sleep(n)    wait n SIMULATED seconds

On real hardware the default source is real time, `now()` is `time.time()` and
`sleep()` is `asyncio.sleep()`. Nothing changes, and no caller needs to know
whether it is being simulated.

## Extending

The source is pluggable. Implement `TimeSource` and install it:

    clock.set_source(MyTimeSource())

Two are provided. `RealTimeSource` is the default. `ScaledTimeSource` runs a fixed
number of simulated seconds per real second, which is what the SimVessel testbench
uses. A future source could advance virtually - stepping straight to the next
scheduled wake rather than sleeping at all - which would run a brew day in seconds
rather than minutes. Nothing outside this module would need to change.
"""

import abc
import asyncio
import time

__all__ = [
    "TimeSource",
    "RealTimeSource",
    "ScaledTimeSource",
    "now",
    "monotonic",
    "sleep",
    "scale",
    "set_scale",
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
