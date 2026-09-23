"""Strike temperature: how hot the water must be before the grain goes in.

Doughing in drops the mash. A sack of room-temperature grain is the largest heat
sink of the brew day, and no feedback controller can prevent the drop - the heat
is gone before the sensor sees it. Measured on a simulated 3-vessel HERMS: an
8.70 F drop, taking 14.5 minutes to recover, with the first minutes of the rest
spent below target where the enzymes wanted to be.

It is entirely predictable, though. The grain mass is in the recipe and its
temperature is the room it has been sitting in, so the water can simply be heated
past the rest temperature by the right amount. That is feedforward, and on the
same model it turns the 8.70 F drop into 1.11 F.

## The arithmetic

Energy the water gives up equals energy the grain takes on:

    m_w c_w (T_strike - T_target) = m_g c_g (T_target - T_grain)

    T_strike = T_target + (m_g c_g) / (m_w c_w) * (T_target - T_grain)

Both sides are temperature *differences*, so the expression holds in Celsius or
Fahrenheit without conversion as long as target and grain temperature share a
unit. Mass and volume must be SI (kg, litres), which is what recipes carry.

Sanity check against the formula brewers actually use - Palmer's
`Tw = T2 + (0.2/r)(T2 - T1)`, with `r` the water-to-grain ratio in quarts per
pound. Converting r to kg/kg gives 2.086, and c_g/c_w = 1700/4186 = 0.406, so
this becomes `T2 + (0.406/2.086r)(T2 - T1)` = `T2 + (0.195/r)(T2 - T1)`. Palmer
says 0.2. The two agree to within 3%, which is the difference between his rounded
constant and the specific heat used here.

## What it deliberately does not model

The tun itself. A cold mash tun absorbs real heat, which is why brewers add a few
degrees by experience, and it depends on the vessel's mass and material. Omitted
rather than guessed: a wrong correction is worse than a known-absent one, and the
controller closes the remaining gap. Expect this to get most of the way there,
not all of it.
"""

__all__ = ["strike_temperature", "StrikeInputs", "GRAIN_SPECIFIC_HEAT", "WATER_SPECIFIC_HEAT"]

#: J/(kg*K). Crushed malt is usually quoted at 1600-1800; the middle is used here
#: and matches the value the SimVessel thermal model uses, so the simulation and
#: this calculation cannot disagree with each other.
GRAIN_SPECIFIC_HEAT = 1700.0

#: J/(kg*K). Close enough for wort as well as water.
WATER_SPECIFIC_HEAT = 4186.0

#: Litres of water per kg - near enough 1 at mash temperatures.
LITRES_TO_KG = 1.0


class StrikeInputs(object):
    """Why a strike temperature could not be computed, or the value if it could.

    Returned rather than raising, because every caller is a control loop that
    must keep going. `ok` false means "use the rest temperature as before".
    """

    def __init__(self, ok, temperature=None, reason=None, clamped=False):
        self.ok = ok
        self.temperature = temperature
        self.reason = reason
        self.clamped = clamped

    def __repr__(self):
        if not self.ok:
            return "<StrikeInputs unavailable: {}>".format(self.reason)
        return "<StrikeInputs {:.2f}{}>".format(
            self.temperature, " clamped" if self.clamped else ""
        )


def strike_temperature(
    target_temp,
    grain_kg,
    grain_temp,
    water_litres,
    max_rise=None,
    absolute_max=None,
):
    """Water temperature needed so the mash lands on target after doughing in.

    All temperatures in one unit, Celsius or Fahrenheit - the expression is in
    differences either way. Returns a StrikeInputs; check `.ok` before using
    `.temperature`.

    `max_rise` and `absolute_max` are safety limits and are the reason this
    returns a result object rather than a float. A strike temperature computed
    from a mistyped grain mass is worse than no feedforward at all: it would
    scald the enzymes before the grain ever went in. The caller supplies limits
    appropriate to its unit; exceeding them clamps and says so rather than
    silently obeying.
    """
    try:
        target = float(target_temp)
        grain_mass = float(grain_kg)
        grain_t = float(grain_temp)
        water = float(water_litres)
    except (TypeError, ValueError):
        return StrikeInputs(False, reason="non-numeric input")

    if grain_mass <= 0:
        return StrikeInputs(False, reason="no grain mass")
    if water <= 0:
        return StrikeInputs(False, reason="no mash water volume")
    if grain_t >= target:
        # Grain at or above the rest temperature would need strike water *below*
        # it. Physically fine, but it means someone has mis-entered something -
        # grain does not sit at mash temperature - and heating less than the
        # target is not a risk worth taking on a guess.
        return StrikeInputs(False, reason="grain is not colder than the target")

    water_mass = water * LITRES_TO_KG
    ratio = (grain_mass * GRAIN_SPECIFIC_HEAT) / (water_mass * WATER_SPECIFIC_HEAT)
    rise = ratio * (target - grain_t)
    strike = target + rise

    clamped = False
    if max_rise is not None and rise > float(max_rise):
        strike = target + float(max_rise)
        clamped = True
    if absolute_max is not None and strike > float(absolute_max):
        strike = float(absolute_max)
        clamped = True

    return StrikeInputs(True, temperature=strike, clamped=clamped)
