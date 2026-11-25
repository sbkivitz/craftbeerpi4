"""A suggestion for a recipe's grain weight and mash water (the strike water to the mash tun) where the recipe has
none of its own, from BeerSmith's data: what a box asking for either opens with, for the brewer to accept with SET.
Nothing here is saved or applied.

The sources, the first that gives the figure:

1. The recipe's own kept BeerSmith file (its basic data's source_xml), read as an import reads it (getMashNumbers):
   its grain bill, and its first infusion's water as BeerSmith shows it ("from the recipe's BeerSmith file").
2. Another Recipe Book recipe of the same name with BeerSmith's own figure (not one typed), the newest, only where
   both grain bills are known and within 2% of each other, so it is the same recipe ("from <name> imported <day>").
3. For the mash water only, the kept file's water-to-grain ratio for its first infusion (WATER_GRAIN_RATIO) times this
   recipe's grain weight ("1.5 qt/lb × 20 lb, BeerSmith's mash ratio").

With none, there's no suggestion: nothing is made up.
"""

import datetime
import logging
import os
import re

import yaml

from cbpi.api import beerxml
from cbpi.strike import model

logger = logging.getLogger(__name__)

#: How far apart two grain bills may be, as a share of this recipe's, for another recipe of the name to count.
GRAIN_MATCH = 0.02
#: Litres per kg in each water-to-grain ratio unit BeerSmith writes.
RATIO_UNITS = {"qt/lb": 0.946352946 / 0.45359237, "l/kg": 1.0, "gal/lb": 3.785411784 / 0.45359237}
WEEK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and n not in (float("inf"), float("-inf")) else None


def _positive(numbers, key):
    n = _number((numbers or {}).get(key)) if isinstance(numbers, dict) else None
    return n if n is not None and n > 0 else None


def own_figure(numbers, key):
    """The recipe's own figure `key` (grain_kg or infuse_l) as BeerSmith gave it: a number more than 0, not one typed
    on Brew Day or in the recipe editor. None without."""
    typed = numbers.get("typed") if isinstance(numbers, dict) else None
    name = {"grain_kg": "grain", "infuse_l": "water"}[key]
    if isinstance(typed, list) and name in typed:
        return None
    return _positive(numbers, key)


def _ratio_l_per_kg(text):
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+\s*/\s*[A-Za-z]+)\s*$", str(text or ""))
    if not m:
        return None
    per = RATIO_UNITS.get(m.group(2).replace(" ", "").lower())
    value = float(m.group(1))
    return value * per if per and value > 0 else None


def read_file(cbpi, basic):
    """The figures of the recipe's kept BeerSmith file (basic data source_xml, relative to the config folder), as an
    import reads them (UploadController.getMashNumbers), with "ratio_l_kg": its first infusion's water-to-grain ratio,
    in L/kg. None without a file, or one that can't be read."""
    from cbpi.controller.upload_controller import UploadController
    rel = basic.get("source_xml") if isinstance(basic, dict) else None
    if not isinstance(rel, str) or not rel.strip():
        return None
    try:
        root_dir = os.path.realpath(cbpi.config_folder.get_file_path(""))
        path = os.path.realpath(os.path.join(root_dir, rel))
        if not path.startswith(root_dir) or not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            root = UploadController._xml_root(f.read())
        recipe = root.find("RECIPE") if root.tag != "RECIPE" else root
        if recipe is None:
            return None
        upload = getattr(cbpi, "upload", None)
        numbers = (upload.getMashNumbers if upload is not None and hasattr(upload, "getMashNumbers")
                   else lambda r: UploadController.getMashNumbers(UploadController, r))(recipe)
        mash = recipe.find("MASH")
        first = None if mash is None else next(
            (s for s in mash.iter("MASH_STEP") if (s.findtext("TYPE") or "").strip().lower() == "infusion"), None)
        ratio = None if first is None else _ratio_l_per_kg(first.findtext("WATER_GRAIN_RATIO"))
        if ratio:
            numbers["ratio_l_kg"] = ratio
        return numbers
    except Exception as e:  # noqa: BLE001 - a suggestion is only a help: without it, the box opens empty
        logger.warning("Strike water: couldn't read the recipe's BeerSmith file %s for a suggestion: %s", rel, e)
        return None


def book_entries(cbpi):
    """(file id, basic data, modified) of each readable Recipe Book recipe."""
    path = cbpi.config_folder.get_file_path("recipes")
    if not os.path.isdir(path):
        return []
    found = []
    for f in os.listdir(path):
        full = os.path.join(path, f)
        if not (os.path.isfile(full) and f.endswith(".yaml")):
            continue
        try:
            with open(full) as file:
                basic = (yaml.load(file, Loader=yaml.FullLoader) or {}).get("basic") or {}
            if isinstance(basic, dict):
                found.append((os.path.splitext(f)[0], basic, os.path.getmtime(full)))
        except Exception:  # noqa: BLE001 - the book page says which can't be read
            continue
    return found


def day_words(epoch):
    d = datetime.datetime.fromtimestamp(epoch)
    return "%s %d %s" % (WEEK[d.weekday()], d.day, MONTH[d.month - 1])


def suggest(cbpi, basic, step_grain_kg, mass_unit, vol_unit, this_file=None, entries=None):
    """{"grain": S or None, "strike_vol": S or None} for the recipe with basic data `basic` (its own grain weight
    elsewhere, a MashIn's Grain_Kg, `step_grain_kg`), each only where the recipe has no figure of its own. S is
    {"value" (in mass_unit or vol_unit), "from": "file" | "book" | "ratio", "says"}, with "name", "file" and
    "imported" (epoch seconds) for "book" and "ratio", "ratio_unit" and "grain" for "ratio". `this_file`: the
    recipe's own Recipe Book file, never its own suggestion. `entries`: book_entries(cbpi), read when needed."""
    numbers = basic.get("mash_numbers") if isinstance(basic, dict) and isinstance(basic.get("mash_numbers"), dict) \
        else {}
    lb = mass_unit == "lb"
    mass = (lambda kg: round(kg / model.LB if lb else kg, 1))
    vol = (lambda litres: round(litres / model.L_PER_GAL if vol_unit == "gal" else litres, 2))
    need_grain = _positive(numbers, "grain_kg") is None
    need_water = _positive(numbers, "infuse_l") is None
    out = {"grain": None, "strike_vol": None}
    if not (need_grain or need_water):
        return out
    kept = read_file(cbpi, basic)
    grain_kg = _positive(numbers, "grain_kg") or step_grain_kg or _positive(kept, "grain_kg")
    if need_grain and _positive(kept, "grain_kg"):
        out["grain"] = dict(value=mass(kept["grain_kg"]), **{"from": "file"}, says="from the recipe's BeerSmith file")
    if need_water and _positive(kept, "infuse_l"):
        out["strike_vol"] = dict(value=vol(kept["infuse_l"]), **{"from": "file"},
                                 says="from the recipe's BeerSmith file")
    if (out["grain"] is None and need_grain) or (out["strike_vol"] is None and need_water):
        name = beerxml.name_key(basic.get("name")) if isinstance(basic, dict) else ""
        others = []
        if name and grain_kg:
            for file_id, other, modified in (book_entries(cbpi) if entries is None else entries):
                if file_id == this_file or beerxml.name_key(other.get("name")) != name:
                    continue
                theirs = other.get("mash_numbers") if isinstance(other.get("mash_numbers"), dict) else {}
                their_grain = own_figure(theirs, "grain_kg")
                if their_grain is None or abs(their_grain - grain_kg) > GRAIN_MATCH * grain_kg:
                    continue
                others.append((modified, file_id, other, theirs))
        for modified, file_id, other, theirs in sorted(others, key=lambda x: x[0], reverse=True):
            said = dict(**{"from": "book"}, name=str(other.get("name")), file=file_id, imported=round(modified),
                        says="from %s imported %s" % (other.get("name"), day_words(modified)))
            if out["grain"] is None and need_grain:
                out["grain"] = dict(value=mass(own_figure(theirs, "grain_kg")), **said)
            water = own_figure(theirs, "infuse_l")
            if out["strike_vol"] is None and need_water and water is not None:
                out["strike_vol"] = dict(value=vol(water), **said)
    ratio = _positive(kept, "ratio_l_kg")
    if out["strike_vol"] is None and need_water and ratio and grain_kg:
        per_shown = ratio / RATIO_UNITS["qt/lb"] if lb else ratio
        unit = "qt/lb" if lb else "L/kg"
        shown = round(per_shown, 2)
        out["strike_vol"] = dict(value=vol(ratio * grain_kg), **{"from": "ratio"}, ratio=shown, ratio_unit=unit,
                                 grain=mass(grain_kg),
                                 says="%s %s × %s %s, BeerSmith's mash ratio" % (
                                     "%g" % shown, unit, "%g" % mass(grain_kg), mass_unit))
    return out
