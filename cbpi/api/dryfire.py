"""Detecting an element heating a vessel that has nothing in it.

Nothing in CraftBeerPi knows whether there is liquid in a kettle. There is no
level sensor, no volume on the kettle, and no check anywhere before an element is
energized. That is survivable while a brewer is driving each step by hand, and it
stops being survivable the moment a profile advances on a timer:

    MashStep.on_timer_done -> next() -> BoilStep.on_start -> AutoMode -> 100%

If the wort has not been transferred yet - which during a sparge it very often
has not - that is several kilowatts into an empty kettle, with nobody present.
The existing guards do not help. A probe in an empty kettle reads air
temperature, which is a perfectly plausible number, so the sensor checks pass;
and the heat-stall watch only ever looks for too LITTLE rise.

The physics, though, is unambiguous. Temperature rises at P / (m * c), so for a
fixed element the rate is inversely proportional to what is in the vessel:

    5.5 kW into 40 L of wort        about   2 C/min
    5.5 kW into a bare 5 kg kettle  about 130 C/min

Roughly sixty-five times apart, and nothing a brewer might plausibly do lands in
between. Half the intended volume is twice the rate; a quarter is four times.
Empty is two orders of magnitude. So a rate ceiling set several times above the
expected rate cannot be reached with any meaningful amount of liquid present,
and is reached almost immediately without it.

This is deliberately the mirror image of the heat-stall watch - that one asks
whether a vessel is rising when it should be, this one asks whether it is rising
faster than is physically possible - and it inherits that feature's hard-won
lesson: a guard that trips on a good brew day is worse than no guard, because
this one cuts the heat. Every choice here is biased towards staying silent:

  - it needs to be told the volume, and does nothing at all until it is
  - it only looks while the element is actually drawing power
  - it needs the rise sustained across a window, not one noisy sample
  - the ceiling is a multiple of the expected rate, not the rate itself
"""

import logging

from cbpi.api import clock

__all__ = ["DryFireWatch"]

# Specific heat of water, J/(kg*K). Wort is within a couple of percent.
WATER_SPECIFIC_HEAT = 4186.0


class DryFireWatch:
    """Watches a vessel's rate of rise against what its contents allow.

    Owned by a kettle logic, which is the only thing that knows both how much
    power it is commanding and what the vessel is supposed to contain.
    """

    #: How many times the expected rate counts as impossible. Four is far above
    #: anything a mis-measured volume produces - it would need three quarters of
    #: the liquid to be missing - and far below the fifty-plus of a dry vessel.
    RATE_MULTIPLE = 4.0

    #: Seconds of sustained excess before acting. Long enough that a probe
    #: glitch, a step change or a stir cannot trip it; short enough that a dry
    #: element has not had time to do real damage. A genuinely empty kettle
    #: exceeds the ceiling continuously from the first sample.
    WINDOW = 20.0

    #: Minimum rise before the rate is even worth computing, in degrees. Below
    #: this, sensor quantisation dominates and the computed rate is noise.
    MIN_RISE = 1.0

    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger(type(self).__name__)
        self.reset()

    def reset(self):
        """Forget everything seen so far."""
        self._anchor_value = None
        self._anchor_at = None
        self._exceeded_since = None
        self.tripped = False

    def expected_rate(self, watts, litres, degree_ratio=1.0):
        """Fastest the vessel can rise with that power and that much liquid.

        `degree_ratio` is 1.8 when the configured unit is Fahrenheit, so the
        answer comes back in whatever degrees the caller is already working in.
        Returns None when there is nothing sensible to compute.
        """
        try:
            watts = float(watts)
            litres = float(litres)
        except (TypeError, ValueError):
            return None
        if watts <= 0 or litres <= 0:
            return None
        return watts * degree_ratio / (litres * WATER_SPECIFIC_HEAT)

    def note(self, value, watts, litres, degree_ratio=1.0):
        """Record a reading. True the moment a dry fire is first detected.

        Returns True exactly once per event, so a caller can act and notify
        without having to remember whether it already did.
        """
        expected = self.expected_rate(watts, litres, degree_ratio)
        if expected is None:
            # No volume configured, or no power being drawn. Nothing to judge
            # against, so judge nothing - and start clean, because a gap in
            # heating makes any rate measured across it meaningless.
            self.reset()
            return False

        try:
            value = float(value)
        except (TypeError, ValueError):
            return False

        now = clock.now()
        if self._anchor_value is None:
            self._anchor_value = value
            self._anchor_at = now
            return False

        elapsed = now - self._anchor_at
        rise = value - self._anchor_value

        if rise < 0:
            # Falling. Whatever was happening is over; measure the recovery from
            # here rather than against a stale low point.
            self._anchor_value = value
            self._anchor_at = now
            self._exceeded_since = None
            return False

        if rise < self.MIN_RISE or elapsed <= 0:
            return False

        rate = rise / elapsed
        ceiling = expected * self.RATE_MULTIPLE

        if rate <= ceiling:
            # Plausible for the liquid it is supposed to contain. Re-anchor so
            # the next measurement is of recent behaviour.
            self._anchor_value = value
            self._anchor_at = now
            self._exceeded_since = None
            return False

        if self._exceeded_since is None:
            self._exceeded_since = now
            return False

        if now - self._exceeded_since < self.WINDOW:
            return False

        if self.tripped:
            return False

        self.tripped = True
        self._logger.error(
            "Dry fire: rising %.2f deg/s with %.0fW into %.1fL, which allows at "
            "most %.2f deg/s. The vessel is probably empty.",
            rate, float(watts), float(litres), ceiling,
        )
        return True

    def describe(self, name, litres):
        """The message to put in front of the brewer."""
        return (
            "'{}' is heating far faster than {:.0f} litres of liquid allows - it "
            "is probably empty. The element has been switched off. Check the "
            "vessel before restarting.".format(name, float(litres))
        )
