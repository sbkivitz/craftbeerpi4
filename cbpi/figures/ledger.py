"""Figures' ledger: the brews as GET /figures sends them, and each figure worked out from them beside what CraftBeerPi
plans with now (BUILDERS.md, C1). Plain data in, plain data out: FiguresController reads the files, settings and
BrewScribe, and the worker the logs.
"""

import math

FIGURES = ("hlt", "strike", "mash", "grain", "boil")
#: Figures USE can apply: each to its setting.
SETTABLE = ("hlt", "strike", "grain")

BUILT_IN_EFFICIENCY = 0.90
L_PER_GAL = 3.785411784
LB_PER_KG = 2.2046226218
#: What water's volume is given at for grain absorption: 68 F.
REFERENCE_C = 20.0
#: A mash rest coming this far over its target, in F, makes the mash figure a caution.
CAME_OVER_F = 0.8


def domain(figure, unit):
    if figure == "hlt":
        return [0.7, 1.0]
    if figure == "strike":
        return [0.45, 1.0]
    if figure == "grain":
        return [0.06, 0.16] if unit == "F" else [0.5, 1.3]
    if figure == "boil":
        return [0.0, 3.5] if unit == "F" else [0.0, 2.0]
    return [0.0, 1.5] if unit == "F" else [0.0, 0.8]


def limit(figure, unit):
    """How far apart the brews may be and still agree."""
    return {"hlt": 0.05, "strike": 0.08}.get(figure) or (
        {"grain": 0.02, "boil": 0.3} if unit == "F" else {"grain": 0.17, "boil": 0.17})[figure]


def number(value):
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def efficiency_fraction(value):
    """(what BrewStart plans with, whether it's the built-in): 85 or 0.85 is 0.85; blank, unreadable or outside
    10-100 % is the built-in 0.90, as BrewStart's own efficiency_fraction reads its setting."""
    n = number(value)
    if n is None:
        return BUILT_IN_EFFICIENCY, True
    fraction = n / 100.0 if n > 1.0 else n
    if 0.1 <= fraction <= 1.0:
        return fraction, False
    return BUILT_IN_EFFICIENCY, True


def water_density(celsius):
    """Water's density in kg/m3 (Kell, 1975), as BrewScribe's boil-off uses it."""
    t = celsius
    return ((999.83952 + 16.945176 * t - 7.9870401e-3 * t ** 2 - 46.170461e-6 * t ** 3 + 105.56302e-9 * t ** 4
             - 280.54253e-12 * t ** 5) / (1 + 16.879850e-3 * t))


def at_68(volume, celsius):
    """The volume as it would measure at 68 F: 10 gal at 168 F is 9.76 gal. With no temperature, as read."""
    if celsius is None:
        return volume
    return volume * water_density(celsius) / water_density(REFERENCE_C)


def _r(x, places):
    return None if x is None or not math.isfinite(x) else round(float(x), places)


def _same(a, b):
    return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6


# ---------------------------------------------------------------------------------------------------------------
# BrewScribe's days
# ---------------------------------------------------------------------------------------------------------------

def to_litres(value, unit):
    return value * L_PER_GAL if unit == "us_gal" else value


def volumes(entries):
    """The volume readings that stand, by stage: the latest of each, leaving out any withdrawn or corrected since."""
    gone = set()
    for e in entries:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("kind") == "withdrawal" and isinstance(data.get("withdraws"), list):
            gone.update(x for x in data["withdraws"] if isinstance(x, str))
        if isinstance(data.get("replaces"), str):
            gone.add(data["replaces"])
    found = {}
    for e in sorted(entries, key=lambda e: e.get("sequence") or 0):
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("kind") != "volume" or e.get("entry_id") in gone:
            continue
        stage = data.get("measurement_stage")
        if stage in ("strike_water", "sparge_water", "pre_boil") and number(data.get("value")) is not None \
                and data.get("unit") in ("us_gal", "l"):
            found[stage] = e
    return found


def entry_celsius(entry):
    data = entry.get("data") or {}
    t = number(data.get("temperature"))
    if t is None or data.get("temperature_unit") not in ("c", "f"):
        return None
    return (t - 32.0) * 5.0 / 9.0 if data["temperature_unit"] == "f" else t


#: Which probe stands for a volume's temperature when its entry has none.
PROBE_FOR = {"strike_water": "hlt", "sparge_water": "hlt", "pre_boil": "boil"}
#: The probes' key for the HLT's reading when its level before the sparge was given (a sparge_water entry's
#: level_before_at, BrewScribe 0.1.0.dev56): <entry id> + BEFORE.
BEFORE = ":level_before"


def grain_cell(found, probes, recipe, unit):
    """A brew's grain absorption from BrewScribe's volumes (`found`, volumes()), the probes' readings at their times
    (`probes`, {entry id: reading in the brewery's unit}, and the HLT's when its level before the sparge was given,
    under <entry id> + BEFORE) and the recipe's mash numbers. Every volume is taken as at 68 F before it's added or
    subtracted. The strike is the HLT's fill less its level before the sparge."""
    fill, sparge, pre = (found.get(s) for s in ("strike_water", "sparge_water", "pre_boil"))
    if not (fill and sparge and pre):
        return {"none": "no_volumes"}
    if number((sparge.get("data") or {}).get("level_before")) is None:
        return {"none": "no_levels"}
    grain_kg = number((recipe or {}).get("grain_kg"))
    if grain_kg is None or grain_kg <= 0:
        return {"none": "no_grain_weight"}
    dead_l = number((recipe or {}).get("lauter_dead_l")) or 0.0

    def celsius(entry, field):
        own = entry_celsius(entry)
        if own is not None:
            return own
        reading = None
        if field == "level_before":
            reading = number((probes or {}).get(str(entry.get("entry_id")) + BEFORE))
        if reading is None:
            reading = number((probes or {}).get(entry.get("entry_id")))
        if reading is None:
            return None
        return (reading - 32.0) * 5.0 / 9.0 if unit == "F" else reading

    def litres(entry, field="value"):
        data = entry["data"]
        return at_68(to_litres(number(data[field]), data["unit"]), celsius(entry, field))

    hlt_fill = litres(fill)
    sparge_before = litres(sparge, "level_before")
    sparge_l = litres(sparge)
    pre_l = litres(pre)
    strike = hlt_fill - sparge_before
    held = strike + sparge_l - pre_l - dead_l
    if unit == "F":
        vol = 1.0 / L_PER_GAL
        grain = grain_kg * LB_PER_KG
    else:
        vol, grain = 1.0, grain_kg
    return {"per": _r(held * vol / grain, 3), "held": _r(held * vol, 2), "strike": _r(strike * vol, 2),
            "hlt_fill": _r(hlt_fill * vol, 2), "sparge_before": _r(sparge_before * vol, 2),
            "strike_from": "hlt_fill", "sparge": _r(sparge_l * vol, 2), "pre_boil": _r(pre_l * vol, 2),
            "dead": _r(dead_l * vol, 2), "grain": _r(grain, 2)}


# ---------------------------------------------------------------------------------------------------------------
# The strike water box's landings (S1)
# ---------------------------------------------------------------------------------------------------------------

def _to_unit(value, from_unit, unit):
    if value is None or from_unit == unit or from_unit not in ("F", "C"):
        return value
    return value * 9.0 / 5.0 + 32.0 if unit == "F" else (value - 32.0) * 5.0 / 9.0


def strike_cell(landing, landed, fit, unit):
    """A brew's strike water cell from its landing (S1's record), where the mash landed, in the brewery's unit, and
    S2's fit for it ({"fit_lo", "fit_hi", "share"}) or None."""
    lo, hi = landing.get("lo") or [], landing.get("hi") or []
    if not lo or not hi or landed is None:
        return {"none": "no_forecast"}
    u = landing.get("unit") if landing.get("unit") in ("F", "C") else unit
    mids = [(number(a) + number(b)) / 2.0 for a, b in list(zip(lo, hi))[5:16]
            if number(a) is not None and number(b) is not None]
    middle = _to_unit(sum(mids) / len(mids), u, unit) if mids else None
    cell = {"landed": _r(landed, 1), "lo": _r(_to_unit(number(lo[0]), u, unit), 1),
            "hi": _r(_to_unit(number(hi[0]), u, unit), 1),
            "off": _r(landed - middle, 1) if middle is not None else None, "t": number(landing.get("t")),
            "fit_lo": None, "fit_hi": None}
    if fit:
        cell["fit_lo"] = _r(_to_unit(number(fit.get("fit_lo")), u, unit), 1)
        cell["fit_hi"] = _r(_to_unit(number(fit.get("fit_hi")), u, unit), 1)
        if number(fit.get("share")) is not None:
            cell["share"] = number(fit["share"])
    return cell


# ---------------------------------------------------------------------------------------------------------------
# The figures
# ---------------------------------------------------------------------------------------------------------------

VALUE_OF = {"hlt": "eff", "grain": "per", "boil": "rate", "strike": "share"}


def values_of(figure, brews, out):
    """Every brew with the figure, left out or not: [{"brew", "v", "out"}]. For mash, one per rest, with its
    target, its v how far it came over."""
    left = set(out.get(figure) or [])
    found = []
    for b in brews:
        cell = (b.get("cells") or {}).get(figure) or {}
        if figure == "mash":
            for r in cell.get("rests") or []:
                found.append({"brew": b["id"], "v": r["over"], "out": b["id"] in left, "target": r["target"]})
            continue
        v = number(cell.get(VALUE_OF[figure]))
        if v is not None:
            found.append({"brew": b["id"], "v": v, "out": b["id"] in left})
    return found


def mash_figure(brews, out, unit, mash_set):
    vals = values_of("mash", brews, out)
    counted_brews = {v["brew"] for v in vals if not v["out"]}
    rests = []
    for b in brews:
        if b["id"] in counted_brews:
            for r in (b["cells"]["mash"].get("rests") or []):
                rests.append((b["id"], r))
    caution_at = CAME_OVER_F if unit == "F" else CAME_OVER_F * 5.0 / 9.0
    came_over = [{"brew": bid, "target": r["target"], "over": r["over"]} for bid, r in rests
                 if (r["over"] or 0) >= caution_at - 1e-9]
    state = "none" if not rests else ("caution" if came_over else "nothing")
    return {"state": state, "n": len(counted_brews),
            "held": _r(max(r["held"] for _, r in rests), 2) if rests else None,
            "over": _r(max(r["over"] for _, r in rests), 2) if rests else None,
            "came_over": came_over, "values": vals, "domain": domain("mash", unit), "set": mash_set,
            "use": None, "why_not": None}


def figure(name, brews, out, unit, now_value, in_use, set_block, why_not=None, fit=None):
    """One figure. `now_value`: what its setting holds now, as the figure is given (efficiency as a fraction), or
    None. `in_use`: the store's record of its USE, still standing (the setting holds what USE set), or None. `fit`:
    for strike, S2's fitted value, or None."""
    vals = values_of(name, brews, out)
    counted = [v for v in vals if not v["out"]]
    n = len(counted)
    places = 2 if name == "boil" else 3
    lo = min(v["v"] for v in counted) if counted else None
    hi = max(v["v"] for v in counted) if counted else None
    if name == "strike":
        measured = number(fit) if fit is not None and n else None
    else:
        measured = sum(v["v"] for v in counted) / n if n else None
    shown = _r(measured, 2) if measured is not None else None
    if n == 0 or (name == "strike" and measured is None):
        state = "none"
    elif n == 1:
        state = "one"
    elif hi - lo > limit(name, unit) + 1e-9:
        state = "disagree"
    elif name == "boil":
        state = "info"
    elif in_use is not None and _same(in_use.get("value"), shown):
        state = "in_use"
    elif in_use is None and _same(now_value, shown):
        # The setting already holds it, though not by USE: nothing to offer.
        state = "in_use"
    else:
        state = "use"
    return {"state": state, "n": n, "measured": _r(measured, places), "lo": _r(lo, places), "hi": _r(hi, places),
            "values": vals, "domain": domain(name, unit), "set": set_block,
            "use": {"value": shown, "brews": [v["brew"] for v in counted]} if state == "use" else None,
            "why_not": why_not}


def standing(record, now_value):
    """The store's USE record, if the setting still holds what it set; else None (changed by hand since)."""
    if not isinstance(record, dict):
        return None
    return record if _same(record.get("value"), now_value) else None


def all_figures(brews, store, unit, settings):
    """Every figure. `settings`: {"hlt": {"registered", "value"}, "grain": {"value"}, "strike": {"available",
    "guess": {"low", "central", "high", "measured"}, "fit", "working"}, "mash": the mash set block or None}. strike
    also says whether S2 is working its fit out ("working")."""
    out = (store or {}).get("out") or {}
    used = (store or {}).get("in_use") or {}

    hs = settings.get("hlt") or {}
    eff, built_in = efficiency_fraction(hs.get("value"))
    hlt_rec = standing(used.get("hlt"), None if built_in else eff)
    hlt_set = {"setting": "BREWSTART_EFFICIENCY", "value": _r(eff, 3), "built_in": built_in,
               "since": hlt_rec["since"] if hlt_rec else None}
    if hlt_rec:
        hlt_set["was"] = hlt_rec.get("was")

    gs = settings.get("grain") or {}
    grain_now = number(gs.get("value"))
    grain_rec = standing(used.get("grain"), grain_now)
    grain_set = {"setting": "GRAIN_ABSORPTION", "value": grain_now, "since": grain_rec["since"] if grain_rec else None}
    if grain_rec:
        grain_set["was"] = grain_rec.get("was")

    ss = settings.get("strike") or {}
    guess = ss.get("guess") or {}
    measured = guess.get("measured") if isinstance(guess.get("measured"), dict) else None
    strike_now = number(measured.get("value")) if measured else None
    strike_rec = standing(used.get("strike"), strike_now) if measured and measured.get("method") == "figures" \
        else None
    strike_set = {"name": "tun_share", "low": number(guess.get("low")), "high": number(guess.get("high")),
                  "central": number(guess.get("central")), "value": strike_now,
                  "since": strike_rec["since"] if strike_rec else None}
    if strike_rec:
        strike_set["was"] = strike_rec.get("was")

    return {
        "hlt": figure("hlt", brews, out, unit, None if built_in else eff, hlt_rec, hlt_set,
                      None if hs.get("registered") else "brewstart_old"),
        "strike": dict(figure("strike", brews, out, unit, strike_now, strike_rec, strike_set,
                              None if ss.get("available") else "no_plant", fit=ss.get("fit")),
                       working=bool(ss.get("working"))),
        "mash": mash_figure(brews, out, unit, settings.get("mash")),
        "grain": figure("grain", brews, out, unit, grain_now, grain_rec, grain_set),
        "boil": figure("boil", brews, out, unit, None, None, None),
    }
