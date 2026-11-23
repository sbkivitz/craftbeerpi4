"""One vessel's logs over a time window, drawn as the brew-day screen's graphs draw them (C2): its probe, its target
where something held one, its element's level and the recipe's step numbers."""

import math

import numpy as np

from cbpi.figures import analysis, logs


def thin(t, v, points):
    """At most about `points` readings: the highest in each slice of time from the first, at the slice's start, and
    always the latest reading, as LogController._thin does."""
    if len(t) <= points:
        return t, v
    span = float(t[-1] - t[0])
    if span <= 0:
        return t[-points:], v[-points:]
    width = max(0.001, span / points)
    k = np.floor((t - t[0]) / width).astype(np.int64)
    starts = np.concatenate(([0], np.flatnonzero(np.diff(k)) + 1))
    highest = np.maximum.reduceat(v, starts)
    times = t[0] + k[starts] * width
    if times[-1] < t[-1]:
        times = np.concatenate((times[:-1], [t[-1]]))
        highest = np.concatenate((highest[:-1], [v[-1]]))
    return times, highest


def _pairs(t, v, places=3):
    return {"t": [round(float(x), 3) for x in t], "v": [round(float(x), places) for x in v]}


def _with_start(s, t0, t1):
    """A change log's rows in t0..t1, led by its value at t0 when it had one."""
    w = s.window(t0, t1)
    before = s.at(t0)
    if before is not None and (not len(w) or w.t[0] > t0):
        return np.concatenate(([t0], w.t)), np.concatenate(([before], w.v))
    return w.t, w.v


def window(folder, params, vessel, t0, t1, points=600):
    """{"temp", "target", "element", "steps"} for `vessel` ("hlt", "mash" or "boil") from t0 to t1, in the brewery's
    unit. "target" is the kettle's target with 0 wherever nothing held it: its AUTO off, and for the HLT the mash
    tun's AUTO off too (the HERMS holds it). "element" is null with no element log, "steps" null with no step log,
    "target" null with no target log."""
    vessels = params.get("vessels") or {}
    own = vessels.get(vessel) or {}

    def read(name):
        if not name:
            return analysis.EMPTY
        t, v = logs.read_series(folder, name)
        return analysis.S(t, v)

    probe = read(own.get("probe")).window(t0, t1)
    tt, tv = thin(probe.t, probe.v, points)
    out = {"temp": _pairs(tt, tv, 2), "target": None, "element": None, "steps": None}

    kettle = own.get("kettle")
    target = read("%s.target" % kettle) if kettle else analysis.EMPTY
    if len(target):
        autos = [read("%s.auto" % kettle)] if kettle else []
        if vessel == "hlt" and (vessels.get("mash") or {}).get("kettle"):
            autos.append(read("%s.auto" % vessels["mash"]["kettle"]))
        autos = [a for a in autos if len(a)]
        times = [t0] + target.window(t0, t1).t.tolist()
        for a in autos:
            times += a.window(t0, t1).t.tolist()
        times = sorted(set(times))
        values = []
        for when in times:
            aim = target.at(when) or 0.0
            held = any((a.at(when) or 0) > 0 for a in autos) if autos else aim > 0
            values.append(aim if held and math.isfinite(aim) else 0.0)
        out["target"] = _pairs(times, values, 2)

    element = own.get("element")
    level = read("%s.out" % element) if element else analysis.EMPTY
    if len(level):
        et, ev = _with_start(level, t0, t1)
        et, ev = thin(np.asarray(et, dtype=float), np.asarray(ev, dtype=float), points)
        out["element"] = _pairs(et, ev, 1)

    steps = read("steps.active")
    if len(steps):
        st, sv = _with_start(steps, t0, t1)
        out["steps"] = [[round(float(a), 3), int(b)] for a, b in zip(st, sv) if b > 0 or a > t0]
    return out
