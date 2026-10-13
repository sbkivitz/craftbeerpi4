"""Whether a sensor reading is one that control may act on.

One definition, shared by the fermenter logics and the fermentation steps.
They used to judge readings differently: the logic held heating and cooling
off for a DS18B20's 85 C power-on value, while a ramp step took the same
reading as its starting point, kept commanding targets from it after the
probe recovered, and the logic - finding those targets plausible - heated a
beer that was being cold-crashed (DEFECTS F15).

A reading is trusted only on affirmative evidence: a finite value, a finite
age within the sensor's own limit, and a value the vessel can have. A sensor
that has never published reports its initial value with no age at all, and
that is "not yet", not "fresh" (F16).
"""

import math

# Temperatures a fermenter, or the glycol that chills one, can actually have.
# Outside this is a probe fault, not a beer: a DS18B20 reads 85.0 C at
# power-on and some drivers report -127 C for a missing device; 900+ is
# CraftBeerPi's own "no reading" sentinel. Glycol held at 25-28 F is well
# inside; a reservoir run below -22 F would need this widened.
FERMENTER_C = (-30.0, 80.0)

# Age limit for a reading whose sensor declares none (SensorController
# normally supplies max_age from the sensor's own publish interval).
DEFAULT_MAX_AGE = 30


def finite(value):
    """`value` as a float, or None when it is not a finite number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def temp_unit(cbpi):
    """'F' or 'C', from the server's TEMP_UNIT; C when it cannot be read."""
    try:
        unit = cbpi.config.get("TEMP_UNIT", "C")
    except Exception:  # noqa: BLE001
        unit = "C"
    return "F" if str(unit).strip().upper().startswith("F") else "C"


def bounds(unit, celsius=FERMENTER_C):
    """`celsius` (low, high) expressed in `unit`."""
    low, high = celsius
    if unit == "F":
        return low * 9 / 5 + 32, high * 9 / 5 + 32
    return low, high


def assess(state, what="temperature", label="", unit=None, celsius=FERMENTER_C,
           default_max_age=DEFAULT_MAX_AGE):
    """(value, None) for a reading control may act on, else (None, (kind, message)).

    `state` is what SensorController.get_sensor_value() returns: a dict with
    value, age and max_age, or None. `unit` and `celsius` bound the value;
    pass celsius=None for a quantity with no plausibility range (pressure).
    """
    name = what.capitalize()
    who = " {}".format(label) if label else ""
    if not isinstance(state, dict):
        return None, ("silent", "{} sensor{} is not reporting".format(name, who))
    raw = state.get("value")
    value = finite(raw)
    if value is None:
        return None, ("value", "{} sensor{} reads {!r}, not a number".format(name, who, raw))
    age = finite(state.get("age"))
    if age is None:
        return None, ("unproven", "{} sensor{} has not reported a reading yet".format(name, who))
    limit = finite(state.get("max_age")) or default_max_age
    if age > limit:
        return None, ("stale", "{} sensor{} has not updated for {:.0f}s".format(name, who, age))
    if celsius is not None:
        low, high = bounds(unit or "C", celsius)
        if not low <= value <= high:
            return None, ("implausible",
                          "{} sensor{} reads {:.1f}{} - no fermenter can be that, so the "
                          "probe has failed".format(name, who, value, unit or "C"))
    return value, None
