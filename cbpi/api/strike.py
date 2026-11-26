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

__all__ = ["strike_temperature", "StrikeInputs", "GRAIN_SPECIFIC_HEAT", "WATER_SPECIFIC_HEAT", "BEERSMITH_MALT_HEAT",
           "steel_counted", "dough_in_temperature", "dough_in_limits", "beersmith_strike", "follows_beersmith"]

import math

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


# ---------------------------------------------------------------------------------------------------------------
# BeerSmith's strike temperature, and the dough-in MashIn's pre-heat from it
# ---------------------------------------------------------------------------------------------------------------
#
# BeerSmith works its strike out with "Adjust Temp for Equip" on as
#
#     W (S_bs - R) = c G (R - Tg) + f Ct (R - Tt)
#
# W the mash water (kg, near enough its litres), S_bs its strike, R its first rest, G and Tg the grain and its
# temperature, c its malt specific heat, Ct the tun's steel as kg of water (TUN_WEIGHT x TUN_SPECIFIC_HEAT), Tt the
# tun's starting temperature, and f the share of the tun the mash fills. So its strike already pays for warming a
# cold tun. On a HERMS the moved water warms the tun's steel and MashIn then tops it up through the coil before the
# malt goes in: heated to S_bs again it would pay for the steel twice, and the mash would land high, where nothing
# can bring it down. MashIn heats the moved water instead to where BeerSmith's strike water settles once it has
# warmed the steel BeerSmith counted:
#
#     T_mi = (W S + Cs Tt) / (W + Cs),   Cs = [W (S_bs - R) - c G (R - Tg)] / (R - Tt), held between 0 and Ct
#
# Cs, the steel BeerSmith counted, is worked out from BeerSmith's own figures rather than taken as the whole tun,
# because BeerSmith counts only the share the mash fills. S is the strike in use, BeerSmith's or the brewer's.

#: BeerSmith's malt specific heat, cal/g C: its advanced option O_MALT_SPECIFIC_HEAT, 0.38 unless changed.
BEERSMITH_MALT_HEAT = 0.38

#: MashInStep's limits for any water it heats before the malt (MAX_STRIKE_RISE_C, MAX_STRIKE_ABS_C), by unit:
#: the most above the rest, and the most at all.
DOUGH_IN_RISE = {"F": 27.0, "C": 15.0}
DOUGH_IN_MAX = {"F": 82.0 * 1.8 + 32.0, "C": 82.0}


def _finite(value):
    """A finite float, or None for anything missing, blank or not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value):
    number = _finite(value)
    return number if number is not None and number > 0 else None


def _in_unit(celsius, unit):
    return None if celsius is None else (celsius * 1.8 + 32.0 if unit == "F" else celsius)


def _beersmith_own(numbers, field, name):
    """BeerSmith's own figure for `field` (grain_kg or infuse_l): the one the recipe editor kept aside when it changed
    it, else the recipe's own unless it was given on Brew Day (named in typed), when BeerSmith gave none."""
    kept = numbers.get("beersmith") if isinstance(numbers.get("beersmith"), dict) else {}
    own = _positive(kept.get(field))
    if own is not None:
        return own
    typed = numbers.get("typed") if isinstance(numbers.get("typed"), list) else []
    return None if name in typed else _positive(numbers.get(field))


def steel_counted(numbers):
    """The tun's steel BeerSmith counted in its strike temperature, as kg of water (Cs above), from a recipe's
    mash_numbers: its mash water and grain weight as BeerSmith gave them, its strike (infuse_temp_c), its first rest
    (rest_c), and the grain's and the tun's temperatures it took. Held between 0 and the whole tun (tun_kg x
    tun_cal_g_c). 0 with "Adjust Temp for Equip" off (equip_adjust False) or no tun: BeerSmith counted no steel.
    None when another figure it needs is missing, as for a recipe imported before rest_c was kept."""
    if not isinstance(numbers, dict):
        return None
    if numbers.get("equip_adjust") is False:
        return 0.0
    tun_kg, heat = _positive(numbers.get("tun_kg")), _positive(numbers.get("tun_cal_g_c"))
    whole = tun_kg * heat if tun_kg is not None and heat is not None else 0.0
    if whole <= 0:
        return 0.0
    water = _beersmith_own(numbers, "infuse_l", "water")
    grain = _beersmith_own(numbers, "grain_kg", "grain")
    strike, rest = _finite(numbers.get("infuse_temp_c")), _finite(numbers.get("rest_c"))
    grain_t, tun_t = _finite(numbers.get("grain_temp_c")), _finite(numbers.get("tun_temp_c"))
    if None in (water, grain, strike, rest, grain_t, tun_t):
        return None
    if rest - tun_t <= 0:
        # A tun at or above the rest takes no heat from the water: BeerSmith counted none.
        return 0.0
    counted = (water * (strike - rest) - BEERSMITH_MALT_HEAT * grain * (rest - grain_t)) / (rest - tun_t)
    return max(0.0, min(whole, counted))


def dough_in_limits(rest, unit):
    """(lowest, highest) MashIn heats the water to before the malt, in `unit`: the rest, and at most 27 F (15 C) above
    it and never over 179.6 F (82 C). None without a rest."""
    rest = _finite(rest)
    if rest is None:
        return None
    return rest, min(rest + DOUGH_IN_RISE[unit], DOUGH_IN_MAX[unit])


def dough_in_temperature(strike, numbers, unit, rest, water_l=None, tun=None):
    """T_mi: what the dough-in MashIn heats the moved strike water to before it asks for the malt, for the strike in
    use `strike`, in `unit` ("F" or "C") as `strike` and `rest` (MashIn's Temp) are, to 0.1. (W S + Cs Tt) / (W + Cs):
    W the mash water in force (`water_l`, else mash_numbers' infuse_l), and for a recipe with BeerSmith's strike,
    Cs the steel it counted (steel_counted) at its tun temperature. For a recipe without BeerSmith's strike, `tun`,
    (kg of water, its temperature in `unit`): the whole mash tun from the Hardware page, at the room's temperature.
    Within dough_in_limits. None when a figure is missing: no BeerSmith figure it needs and no `tun`, no mash water,
    no strike or no rest."""
    numbers = numbers if isinstance(numbers, dict) else {}
    strike, rest = _finite(strike), _finite(rest)
    limits = dough_in_limits(rest, unit)
    water = _positive(water_l) if water_l is not None else _positive(numbers.get("infuse_l"))
    if strike is None or limits is None or water is None:
        return None
    if _finite(numbers.get("infuse_temp_c")) is not None:
        steel = steel_counted(numbers)
        tun_t = _in_unit(_finite(numbers.get("tun_temp_c")), unit)
        if steel is None or (steel > 0 and tun_t is None):
            return None
        tun_t = tun_t if tun_t is not None else 0.0
    else:
        if not tun or _finite(tun[0]) is None or _finite(tun[1]) is None or tun[0] < 0:
            return None
        steel, tun_t = float(tun[0]), float(tun[1])
    settled = (water * strike + steel * tun_t) / (water + steel)
    low, high = limits
    if high < low:
        return None
    return round(max(low, min(high, settled)), 1)


def beersmith_strike(numbers, unit):
    """BeerSmith's strike temperature (mash_numbers' infuse_temp_c) in `unit`, to 0.1, or None."""
    if not isinstance(numbers, dict):
        return None
    value = _in_unit(_finite(numbers.get("infuse_temp_c")), unit)
    return None if value is None else round(value, 1)


def follows_beersmith(value, old_numbers, new_numbers, unit):
    """For an UPDATE import: the new BeerSmith strike for a strike water temperature `value` that still holds the old
    one (to 0.1 in `unit`), or None to leave it as it is - a figure of the brewer's own, or no BeerSmith figure in
    either."""
    held, old, new = _finite(value), beersmith_strike(old_numbers, unit), beersmith_strike(new_numbers, unit)
    if held is None or old is None or new is None or abs(round(held, 1) - old) > 1e-6 or abs(new - old) < 1e-6:
        return None
    return new