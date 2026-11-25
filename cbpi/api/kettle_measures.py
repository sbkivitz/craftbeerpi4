"""What a kettle measures, kept with the kettle (kettle.json, GET and PUT /kettle, its settings file).

- capacity: how much it holds, in the brewery's volume unit, gallons on a F brewery, litres on a C one;
- min_water: the least water that keeps its element, and its coil if it has one, covered, in that volume unit too;
- empty_weight: what it weighs empty, in pounds on a F brewery, kilograms on a C one;
- material: "stainless_steel", "aluminium" or "other";
- specific_heat: with "other", its specific heat in cal/g.C, as BeerSmith's TUN_SPECIFIC_HEAT (stainless steel is
  0.12, aluminium 0.22).

Each may be blank, meaning unknown, and a kettle without them works as one always has: nothing is refused for a
missing or odd value. A value is kept as it was given, for the Hardware page to show and flag, and every use reads
it through usable() or material(), so a value that can't be right - zero, negative, not a number - counts as blank.
"""

import math

FIELDS = ("capacity", "min_water", "empty_weight", "material", "specific_heat")
NUMBERS = ("capacity", "min_water", "empty_weight", "specific_heat")
MATERIALS = ("stainless_steel", "aluminium", "other")


def stored(field, value):
    """`value` as kettle.json keeps the measurement `field`: None for blank (None, or text of only spaces); a finite
    number as it is; text of a number, for a number field, as that number; a material by its name in MATERIALS when
    material() knows it; any other text as it is, trimmed, for the Hardware page to flag. Anything else - true, a
    list, NaN - is blank. Never raises."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if field in NUMBERS and math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if field in NUMBERS:
        try:
            number = float(text.replace(",", "."))
        except ValueError:
            return text
        return number if math.isfinite(number) else text
    if field == "material":
        return material(text) or text
    return text


def usable(value):
    """A measurement to use: a number above 0, else None (blank, zero, negative, not a number)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value.strip().replace(",", ".")) if isinstance(value, str) else float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def material(value):
    """One of MATERIALS, or None. Matched without regard to case, spaces around it, or a space or hyphen for the
    underscore: "Stainless Steel" is "stainless_steel"."""
    if not isinstance(value, str):
        return None
    word = "_".join(value.strip().lower().replace("-", " ").split())
    return word if word in MATERIALS else None


def of(item):
    """{field: value} of the measurements `item` (a Kettle, or anything with those attributes) has, blank ones left
    out: {} for a kettle without any."""
    found = {}
    for field in FIELDS:
        value = getattr(item, field, None)
        if value is not None:
            found[field] = value
    return found
