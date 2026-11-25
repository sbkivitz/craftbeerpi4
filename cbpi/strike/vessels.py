"""What the strike water model takes from a vessel's own figures, kept with its kettle on the Hardware page
(cbpi.api.kettle_measures): what it weighs empty, and what it's made of; and the least water the HLT keeps over its
element and coil (least_water), which the strike water may never go below.

The rule is the brewer's: a vessel's figure only narrows a guess or replaces BeerSmith's. A weight or material that
is blank, missing, 0 or less, or can't be read gives nothing, and the model is exactly as it is without it.

- A usable empty_weight with material stainless steel or aluminium: the weight at its specific heat, 0.12 or 0.22
  cal/g C, as BeerSmith's TUN_SPECIFIC_HEAT takes it.
- With material "other": the weight at the kettle's own specific_heat, when that is more than 0 and no more than
  water's, 1 cal/g C - nothing a kettle is made of holds more; else nothing.
- With no material: the weight taken as stainless steel.
- A material the core doesn't know, or a material with no usable weight: nothing.

The Mash Tun's figure takes the place of the recipe's tun (BeerSmith's TUN_WEIGHT and TUN_SPECIFIC_HEAT). The HLT's
fixes the HLT STEEL guess, unless the plant file has that measured, which wins.
"""

import dataclasses

from cbpi.strike import model

try:
    from cbpi.api import kettle_measures
except ImportError:  # a core without the kettles' figures: no vessel gives one
    kettle_measures = None

#: cal/g C each material the Hardware page offers holds, as BeerSmith's TUN_SPECIFIC_HEAT.
SPECIFIC_HEAT = {"stainless_steel": 0.12, "aluminium": 0.22}
#: Water's: more, for what a kettle is made of, is a figure mistyped.
MAX_SPECIFIC_HEAT = 1.0
#: The least water an HLT keeps over its element and coil where Hardware gives no min_water: this share of its capacity,
#: else this much, by volume unit.
LEAST_SHARE = 0.15
LEAST_DEFAULT = {"gal": 3.0, "L": 11.4}


def figure(kettle, unit):
    """{"empty_weight": as the kettle has it, in the brewery's mass unit (lb on a F brewery, kg on a C one), "kg",
    "material": the kettle's, or None when it has none, "specific_heat": cal/g C} from a kettle's own figures, or
    None when they give nothing. Never raises."""
    if kettle is None or kettle_measures is None:
        return None
    try:
        weight = kettle_measures.usable(getattr(kettle, "empty_weight", None))
        if weight is None:
            return None
        raw = getattr(kettle, "material", None)
        blank = raw is None or (isinstance(raw, str) and not raw.strip())
        material = None if blank else kettle_measures.material(raw)
        if not blank and material is None:
            return None  # a material it can't put a number to
        if material == "other":
            heat = kettle_measures.usable(getattr(kettle, "specific_heat", None))
            if heat is None or heat > MAX_SPECIFIC_HEAT:
                return None
        else:
            heat = SPECIFIC_HEAT[material or "stainless_steel"]
    except Exception:  # noqa: BLE001 - a figure that can't be read is no figure
        return None
    return dict(empty_weight=weight, kg=weight * model.LB if unit == "F" else weight, material=material,
                specific_heat=heat)


def least_water(kettle, unit, vol_unit):
    """{"value": the least water the kettle keeps, in vol_unit ("gal" or "L"), to 2 places, "from": "hardware",
    "capacity" or "default", "capacity": its capacity in vol_unit, or None}: the water that keeps its element, and its
    coil if it has one, covered. Never None, never raises.

    - "hardware": the kettle's own min_water on the Hardware page, when it is a number above 0;
    - "capacity": else LEAST_SHARE of its capacity on the Hardware page: an element sits low in a kettle, so that
      share covers it in any common kettle (3 gal of a 20 gal HLT);
    - "default": else LEAST_DEFAULT, a cautious figure for the common 15-20 gal HLT, used only where Hardware gives
      neither.

    Both figures are kept in the brewery's volume unit (gal on a F brewery, L on a C one)."""
    kept = "gal" if unit == "F" else "L"

    def to_vol(value):
        if value is None:
            return None
        if kept == vol_unit:
            return value
        return value * model.L_PER_GAL if vol_unit == "L" else value / model.L_PER_GAL

    own = capacity = None
    if kettle is not None and kettle_measures is not None:
        try:
            own = kettle_measures.usable(getattr(kettle, "min_water", None))
            capacity = kettle_measures.usable(getattr(kettle, "capacity", None))
        except Exception:  # noqa: BLE001 - a figure that can't be read is no figure
            own = capacity = None
    capacity = to_vol(capacity)
    if own is not None:
        value, source = to_vol(own), "hardware"
    elif capacity is not None:
        value, source = capacity * LEAST_SHARE, "capacity"
    else:
        value, source = LEAST_DEFAULT[vol_unit if vol_unit in LEAST_DEFAULT else kept], "default"
    return {"value": round(value, 2), "from": source, "capacity": None if capacity is None else round(capacity, 2)}


def with_hlt(plant, found):
    """(plant, where the HLT's steel comes from): with the HLT's figure `found`, the HLT STEEL guess fixed at the
    figure's worth in the model's steel (kg at model.STEEL_C), "hardware"; the plant as it is with the guess measured
    in the plant file ("measured") or no figure ("guess")."""
    r = plant.guesses.get("hlt_steel")
    if r is not None and r.provenance == model.MEASURED:
        return plant, "measured"
    if found is None or r is None:
        return plant, "guess"
    kg = round(found["kg"] * found["specific_heat"] * model.CAL_G_C / model.STEEL_C, 3)
    fixed = dataclasses.replace(r, central=kg, low=kg, high=kg, provenance=model.DERIVED,
                                note="the HLT's steel and coil, from its weight and material on the Hardware page")
    return dataclasses.replace(plant, guesses=dict(plant.guesses, hlt_steel=fixed)), "hardware"


def shown(mt, hlt, hlt_from, tun, unit):
    """The state's `vessels`: where the tun's and the HLT's steel come from, and the figures the model uses. `mt` and
    `hlt` are figure()'s, `hlt_from` with_hlt()'s, and `tun` (kg, cal/g C) the recipe's tun as the model would count
    it without Hardware, or None."""
    none = {"from": None, "empty_weight": None, "material": None, "specific_heat": None}

    def own(found):
        return {"from": "hardware", "empty_weight": round(found["empty_weight"], 3), "material": found["material"],
                "specific_heat": found["specific_heat"]}

    if mt is not None:
        tun_shown = own(mt)
    elif tun is not None:
        kg, heat = tun
        tun_shown = dict(none, **{"from": "beersmith", "empty_weight": round(kg / model.LB if unit == "F" else kg, 1),
                                  "specific_heat": heat})
    else:
        tun_shown = dict(none)
    hlt_shown = own(hlt) if hlt_from == "hardware" else dict(none, **{"from": hlt_from})
    return {"mt": tun_shown, "hlt": hlt_shown}
