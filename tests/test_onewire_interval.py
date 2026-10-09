"""The declared Interval default and the one actually used must agree.

They did not. `Property.Select(label="Interval", ...)` was changed to
default_value=1 in April 2025; the lookup in start() kept the 60 it was given
in March 2023. A sensor made in the UI therefore published every second,
while a sensor whose props lacked the key - an older config, a hand-edited
sensor.json, a restored backup, a test fixture - published every minute.

That alone would be a performance surprise. It is a functional fault because
SensorController.expected_max_age() returns a 30 s floor for a sensor that
declares no interval: it reads props directly and cannot know any sensor's
private fallback. So the 60 s sensor was judged against a 30 s budget and was
stale by construction - is_fresh() answered False essentially always, and
anything gated on freshness refused to act on a probe that was working.

Nothing exercised the fallback, which is why a 60x discrepancy sat in the
file for eighteen months.
"""

from cbpi.controller.sensor_controller import SensorController
from cbpi.extension.onewire import DEFAULT_INTERVAL, OneWire


def declared_interval_default():
    """The default the dialog seeds, read from the plugin's own metadata."""
    for parameter in OneWire.cbpi_parameters:
        if getattr(parameter, "label", None) == "Interval":
            return parameter.default_value
    raise AssertionError("OneWire no longer declares an Interval parameter")


def test_absent_interval_uses_the_declared_default():
    """A sensor with no Interval key must behave like the dialog's default."""
    sensor = OneWire(None, "test-sensor", {})
    assert sensor.resolve_interval() == declared_interval_default()


def test_declared_default_matches_the_module_constant():
    """Guards the other direction: the Select must not be edited alone."""
    assert declared_interval_default() == DEFAULT_INTERVAL


def test_explicit_interval_still_wins():
    """The fallback must not override a value the user actually chose."""
    sensor = OneWire(None, "test-sensor", {"Interval": 30})
    assert sensor.resolve_interval() == 30


def test_fallback_is_fresh_enough_for_the_staleness_floor():
    """The real failure was the interaction, so assert on that directly.

    A sensor publishing slower than SensorController's floor is stale the
    moment it is created. Whatever the fallback becomes, it has to clear that
    bar, and this fails if someone raises DEFAULT_INTERVAL past the floor
    without also revisiting expected_max_age().
    """
    sensor = OneWire(None, "test-sensor", {})
    assert sensor.resolve_interval() <= SensorController.DEFAULT_MAX_AGE
