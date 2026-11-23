"""What each brew did, worked out from one folder of logs: where the brews are, and each one's HLT heating, mash
rests and boil kettle, as Figures (BUILDERS.md, C1) defines them.

Everything here works in F, whatever the brewery's unit: the limits it uses are F (a heat-up rises 15 F at 0.5 F a
minute, a boil passes 200 F, ...). `Analysis` converts a C brewery's readings on the way in and its answers on the way
out. Times are seconds on CraftBeerPi's clock, as the logs are stamped.

What counts as a brew. Heating is found in the logs as stretches: the HLT heating up (a rise of at least 15 F at
0.5 F a minute or more), a mash rest held, the mash taken past 120 F and the boil kettle past 200 F from below, and,
to place a brew's start and end, any probe warming. Stretches less than 3 hours apart are one brew. A brew counts
when the HLT heated up by itself (the wort pump off), the boil kettle was taken past 200 F, or the mash was taken past
120 F with a rest held or the HLT heating it. So a run that held the mash at 90-116 F with the HLT heating it is not
a brew, and nor is a vessel that starts its logs hot and cools; an HLT heated by hand is.

Where BrewScribe has a brew day, that day is the brew: `days` gives each one's window, and the stretches in it make
that brew, in place of any found in the logs there.
"""

import math

import numpy as np

from cbpi.figures import logs

#: Changes whenever what this works out changes, so results kept from an earlier version are worked out again.
VERSION = 2

HEAT_RISE_F = 15.0
HEAT_RATE_F = 0.5
HEAT_MIN_S = 300.0
WARM_RISE_F = 5.0
WARM_RATE_F = 0.2
#: A step this big between two 30-second means is a simulated vessel set to a new temperature, not heat.
JUMP_F = 6.0
MASH_HOT_F = 120.0
BOIL_PAST_F = 200.0
BOIL_AT_F = 211.5
BOIL_FROM_F = 120.0
BREW_GAP_S = 3 * 3600.0
REST_LOWEST_F = 90.0
REST_BAND_F = 1.0
REST_WINDOW_S = 480.0
REST_MIN_S = 300.0
ARRIVE_F = 0.25
LEFT_F = 0.3
HELD_BAND_F = 2.0
#: The mash rising by this share of the HLT's rise means the wort pump ran through the coil.
COUPLED_SHARE = 0.25
BUCKET_S = 30.0

WATER_J_PER_KG_K = 4181.0
WATER_KG_PER_L = 0.995
BUILT_IN_EFFICIENCY = 0.90
L_PER_GAL = 3.785411784

VESSELS = ("hlt", "mash", "boil")


class S:
    """A log's readings: sorted times and their values."""

    __slots__ = ("t", "v")

    def __init__(self, t, v):
        self.t, self.v = t, v

    def __len__(self):
        return len(self.t)

    def window(self, t0, t1):
        i = int(np.searchsorted(self.t, t0, "left"))
        j = int(np.searchsorted(self.t, t1, "right"))
        return S(self.t[i:j], self.v[i:j])

    def at(self, when):
        """The latest value at or before `when`, or None."""
        i = int(np.searchsorted(self.t, when, "right")) - 1
        return float(self.v[i]) if i >= 0 else None

    def near(self, when, within=None):
        """The reading at or before `when`, else the first after it; None with none (or none within `within` s)."""
        if not len(self):
            return None
        i = int(np.searchsorted(self.t, when, "right")) - 1
        if i < 0:
            i = 0
        if within is not None and abs(self.t[i] - when) > within:
            j = min(i + 1, len(self) - 1)
            if abs(self.t[j] - when) > within:
                return None
            i = j
        return float(self.v[i])


EMPTY = S(np.zeros(0), np.zeros(0))


def to_f(v, unit):
    return v * 9.0 / 5.0 + 32.0 if unit == "C" else v


def from_f(v, unit):
    return None if v is None else ((v - 32.0) * 5.0 / 9.0 if unit == "C" else v)


def delta_from_f(d, unit):
    return None if d is None else (d * 5.0 / 9.0 if unit == "C" else d)


def _r(x, places):
    return None if x is None or not math.isfinite(x) else round(float(x), places)


def bucket(s, step=BUCKET_S):
    """Means over `step`-second slices, at each slice's middle: only slices with readings."""
    if not len(s):
        return EMPTY
    k = np.floor(s.t / step).astype(np.int64)
    starts = np.concatenate(([0], np.flatnonzero(np.diff(k)) + 1))
    sums = np.add.reduceat(s.v, starts)
    counts = np.diff(np.concatenate((starts, [len(k)])))
    return S(k[starts] * step + step / 2.0, sums / counts)


def on_intervals(level, t0=-math.inf, t1=math.inf):
    """[(start, end)] where a level log (a switch's .out, a kettle's .auto) was above 0, clipped to t0..t1. The last
    one still on ends at t1."""
    out, start = [], None
    if not len(level):
        return out
    before = level.at(t0) if math.isfinite(t0) else None
    if before is not None and before > 0:
        start = t0
    w = level.window(t0, t1)
    for t, v in zip(w.t.tolist(), w.v.tolist()):
        if v > 0 and start is None:
            start = t
        elif v <= 0 and start is not None:
            out.append((start, t))
            start = None
    if start is not None:
        out.append((start, t1))
    return out


def _runs(mask):
    """[(first, last)] index runs where mask is true."""
    if not len(mask):
        return []
    m = np.concatenate(([False], mask, [False]))
    d = np.diff(m.astype(np.int8))
    return list(zip(np.flatnonzero(d == 1).tolist(), (np.flatnonzero(d == -1) - 1).tolist()))


def rising(b, rate, rise, min_s=HEAT_MIN_S, lag=4, raw=None):
    """[(t0, t1)] where the 30-second means `b` rose at `rate` F a minute or more over each 2 minutes, by `rise` F or
    more in all, for `min_s` or longer, without a jump. With the readings themselves (`raw`), t0 is the last reading
    before the rise, not the middle of a 30-second slice."""
    n = len(b)
    if n <= lag:
        return []
    t, v = b.t, b.v
    dt = t[lag:] - t[:-lag]
    dv = v[lag:] - v[:-lag]
    steps = np.abs(np.diff(v))
    jumps = np.lib.stride_tricks.sliding_window_view(steps, lag).max(axis=1) > JUMP_F
    gaps = np.lib.stride_tricks.sliding_window_view(np.diff(t), lag).max(axis=1) > 3 * BUCKET_S
    with np.errstate(divide="ignore", invalid="ignore"):
        ok = (dt > 0) & ~jumps & ~gaps & (dv / (dt / 60.0) >= rate)
    out = []
    for a, z in _runs(ok):
        i0, i1 = a, z + lag
        while i0 < i1 and v[i0 + 1] <= v[i0] + 0.02:
            i0 += 1
        while i1 > i0 and v[i1] <= v[i1 - 1] + 0.02:
            i1 -= 1
        if v[i1] - v[i0] >= rise and t[i1] - t[i0] >= min_s:
            t0 = float(t[i0])
            if raw is not None:
                w = raw.window(t[i0] - BUCKET_S / 2.0, t[min(i0 + 1, n - 1)] + BUCKET_S / 2.0)
                above = np.flatnonzero(w.v > w.v[0] + 0.05) if len(w) else []
                if len(above):
                    t0 = float(w.t[max(0, above[0] - 1)])
            out.append((t0, float(t[i1])))
    return out


def mid_rate(seg):
    """The rise over the middle 80 % of a climb, in F a minute: from where it was a tenth of the way up to nine
    tenths."""
    if len(seg) < 3:
        return None
    first, last = float(seg.v[0]), float(seg.v[-1])
    rise = last - first
    if rise <= 0:
        return None
    lo, hi = first + 0.1 * rise, first + 0.9 * rise
    i_lo = int(np.argmax(seg.v >= lo))
    i_hi = int(np.argmax(seg.v >= hi))
    minutes = (seg.t[i_hi] - seg.t[i_lo]) / 60.0
    return 0.8 * rise / minutes if minutes > 0 else None


def rise_start(b, when, lag_s=90.0):
    """Where the climb into `when` began: walking back from `when` through the 30-second means while each is lower
    than the one after it."""
    if not len(b):
        return when
    i = int(np.searchsorted(b.t, when, "right")) - 1
    if i < 0:
        return when
    while i > 0 and b.v[i - 1] < b.v[i] - 0.02 and b.t[i] - b.t[i - 1] <= lag_s:
        i -= 1
    return min(when, float(b.t[i]))


class Analysis:
    """One folder of logs: its readings (in F), the brews in it and what each did.

    `params`: {"unit": "F" | "C", "vessels": {"hlt" | "mash" | "boil": {"probe", "kettle", "element", "pump"}},
    "hlt_sim": {"litres", "watts"} or None (the HLT's probe is a SimVessel: its Volume and Heater_Watts)}. The mash
    tun's "pump" is the wort pump; its "element" is the HLT's on a HERMS."""

    def __init__(self, folder, params, cache=None):
        self.folder = folder
        self.unit = "C" if params.get("unit") == "C" else "F"
        self.vessels = params.get("vessels") or {}
        self.hlt_sim = params.get("hlt_sim")
        self.cache = cache
        self._series = {}

    # Reading ------------------------------------------------------------------------------------------------------

    def series(self, name, temperature=False):
        if not name:
            return EMPTY
        key = (name, temperature)
        if key not in self._series:
            t, v = logs.read_series(self.folder, name, self.cache)
            if temperature and self.unit == "C":
                v = to_f(v, "C")
            self._series[key] = S(t, v)
        return self._series[key]

    def probe(self, vessel):
        return self.series((self.vessels.get(vessel) or {}).get("probe"), temperature=True)

    def change(self, vessel, what):
        """A vessel's change log: "target" or "auto" (its kettle), "element" or "pump" (an actor's level, .out)."""
        v = self.vessels.get(vessel) or {}
        if what in ("target", "auto"):
            kettle = v.get("kettle")
            return self.series("%s.%s" % (kettle, what), temperature=(what == "target")) if kettle else EMPTY
        actor = v.get(what)
        return self.series("%s.out" % actor) if actor else EMPTY

    def names(self):
        """Every log this reads, for the folder's fingerprint."""
        found = ["steps.active"] + ["brewstart.%s" % k for k in
                                    ("planned_min", "ready_by", "litres", "watts", "efficiency", "ready")]
        for v in self.vessels.values():
            if v.get("probe"):
                found.append(v["probe"])
            if v.get("kettle"):
                found += ["%s.target" % v["kettle"], "%s.auto" % v["kettle"]]
            for actor in (v.get("element"), v.get("pump")):
                if actor:
                    found.append("%s.out" % actor)
        return sorted(set(found))

    def nothing_heated(self):
        """True when no probe's readings in this folder span 10 F or more: nothing was heated or cooled here, so there
        is no brew, and the times needn't be read."""
        for vessel in VESSELS:
            name = (self.vessels.get(vessel) or {}).get("probe")
            if not name:
                continue
            span = logs.value_range(self.folder, name)
            if span is not None and to_f(span[1], self.unit) - to_f(span[0], self.unit) >= 10.0:
                return False
        return True

    def span(self):
        ts = [self.probe(v).t for v in VESSELS if len(self.probe(v))]
        if not ts:
            return None
        return [float(min(t[0] for t in ts)), float(max(t[-1] for t in ts))]

    # Finding the stretches --------------------------------------------------------------------------------------

    def heatups(self):
        """The HLT's heat-ups: [{"t0", "t1", "from", "to", "alone"}]. Each starts where its element went on (or where
        the probe began to rise, with no element log), and ends at the first of: the reading at the HLT's target less
        0.5 F, the wort pump going on, the element going off, the rise ending. "alone": the wort pump was off from its
        start (by the pump's log, or, with none, because the mash didn't follow the HLT up). "seen": the logs begin
        before it, so when it began is known."""
        if hasattr(self, "_heatups"):
            return self._heatups
        hlt = self.probe("hlt")
        b = bucket(hlt)
        element = self.change("hlt", "element")
        pump = self.change("mash", "pump")
        target = self.change("hlt", "target")
        mash = self.probe("mash")
        found = []
        for ts, te in rising(b, HEAT_RATE_F, HEAT_RISE_F, raw=hlt):
            t0 = ts
            if len(element):
                ons = [a for a, z in on_intervals(element, ts - 1800, te) if a <= ts + 60 and z > ts]
                if ons:
                    t0 = max(ons[-1], ts - 1800)
            t1 = te
            w = hlt.window(t0, te)
            if len(target):
                tw = target.window(t0, te)
                times = np.concatenate(([t0], tw.t))
                aims = np.concatenate(([target.at(t0) or 0.0], tw.v))
                for k in range(len(times)):
                    aim = aims[k]
                    if aim <= 0:
                        continue
                    end = times[k + 1] if k + 1 < len(times) else te
                    seg = w.window(times[k], end)
                    hit = np.flatnonzero(seg.v >= aim - 0.5)
                    if len(hit):
                        t1 = min(t1, float(seg.t[hit[0]]))
                        break
            if len(element):
                offs = [(a, z) for a, z in on_intervals(element, t0, te) if a <= t0 + 60]
                if offs and offs[0][1] < t1:
                    t1 = offs[0][1]
            pump_known = len(pump) and pump.at(t0 + 1) is not None
            alone = True
            if pump_known:
                if (pump.at(t0 + 1) or 0) > 0:
                    alone = False
                else:
                    for a, _ in on_intervals(pump, t0, te):
                        if a < t1:
                            t1 = a
                        break
            start_v = hlt.near(t0)
            end_v = hlt.near(t1)
            if start_v is None or end_v is None:
                continue
            # The mash following the HLT up means the wort pump ran, whatever its log says (an older run has none,
            # and a stale one says off).
            m0, m1 = mash.near(t0, 600), mash.near(t1, 600)
            if m0 is not None and m1 is not None and (m1 - m0) >= COUPLED_SHARE * max(end_v - start_v, 1e-9):
                alone = False
            # Heating already under way where the logs begin: when it began isn't known, so it gives no figure.
            seen = bool(len(hlt)) and float(hlt.t[0]) < t0 - 60.0
            found.append({"t0": float(t0), "t1": float(t1), "from": start_v, "to": end_v, "alone": alone,
                          "seen": seen, "rise_end": float(te)})
        self._heatups = found
        return found

    def warming(self):
        """Every probe's warming stretches: [(t0, t1)]. They place a brew's start and end, and count for nothing."""
        out = []
        for vessel in VESSELS:
            out += rising(bucket(self.probe(vessel)), WARM_RATE_F, WARM_RISE_F, raw=self.probe(vessel))
        return out

    def hot(self, vessel, above):
        """[(t0, t1)] where a probe's 30-second means went to `above` F or more from below, until they fell back: a
        vessel already hot where its logs begin wasn't heated here."""
        b = bucket(self.probe(vessel))
        out = []
        for a, z in _runs(b.v >= above):
            if a > 0 and b.v[a - 1] < above and b.t[a] - b.t[a - 1] <= 3 * BUCKET_S:
                out.append((float(b.t[a]), float(b.t[z])))
        return out

    def boil_climbs(self):
        """[(t_cross, t_hot_end)]: each time the boil kettle went past 200 F from below, to where it fell back."""
        return self.hot("boil", BOIL_PAST_F)

    def plateaus(self):
        """Where the mash stayed within 1 F for 8 minutes or more, at 90 F or more, having been heated to it (it was
        2 F lower in the 45 minutes before) or held there by the mash tun's AUTO: [(t0, t1, median)]."""
        if hasattr(self, "_plateaus"):
            return self._plateaus
        mash = self.probe("mash")
        b = bucket(mash)
        w = int(REST_WINDOW_S // BUCKET_S)
        found = []
        if len(b) > w:
            win = np.lib.stride_tricks.sliding_window_view(b.v, w)
            tw = np.lib.stride_tricks.sliding_window_view(b.t, w)
            ok = ((win.max(axis=1) - win.min(axis=1)) <= REST_BAND_F) & (win.mean(axis=1) >= REST_LOWEST_F) & \
                 ((tw[:, -1] - tw[:, 0]) <= w * BUCKET_S * 1.5)
            covered = np.zeros(len(b), dtype=bool)
            for i in np.flatnonzero(ok):
                covered[i:i + w] = True
            auto = self.change("mash", "auto")
            target = self.change("mash", "target")
            for a, z in _runs(covered):
                t0, t1 = float(b.t[a]), float(b.t[z])
                median = float(np.median(mash.window(t0, t1).v))
                before = b.window(t0 - 2700, t0)
                heated = len(before) and float(before.v.min()) <= median - 2.0
                held = len(auto) and (auto.at(t0) or 0) > 0 and target.at(t0) is not None \
                    and abs(target.at(t0) - median) <= 2.0
                if heated or held:
                    found.append((t0, t1, median))
        self._plateaus = found
        return found

    # A brew's figures -------------------------------------------------------------------------------------------

    def rest_target(self, t0, t1, median):
        target = self.change("mash", "target")
        auto = self.change("mash", "auto")
        for when in (t0, (t0 + t1) / 2.0, t1):
            aim = target.at(when) if len(target) else None
            on = (auto.at(when) or 0) > 0 if len(auto) else True
            if aim is not None and aim > 0 and on and abs(aim - median) <= 2.0:
                return aim
        # No target logged: the rest's temperature, to the whole degree in the brewery's unit.
        return to_f(round(from_f(median, self.unit)), self.unit)

    def rests(self, t_from, t_to):
        """The rests held between t_from and t_to, each measured as BUILDERS.md says: [{"target", "t0", "t1",
        "minutes", "held", "over", "last_degree"}] in F."""
        mash = self.probe("mash")
        out = []
        last_end = t_from
        for p0, p1, median in self.plateaus():
            if p1 < t_from or p0 > t_to or p1 <= last_end:
                continue
            target = self.rest_target(p0, p1, median)
            search = max(last_end, p0 - 2700.0)
            seg = mash.window(search, min(p1, t_to))
            if not len(seg):
                continue
            above = seg.v[0] > target + ARRIVE_F
            hit = np.flatnonzero(np.abs(seg.v - target) <= ARRIVE_F)
            if not len(hit):
                continue
            arrive = float(seg.t[hit[0]])
            after = mash.window(arrive, t_to)
            outside = np.flatnonzero(np.abs(after.v - target) > HELD_BAND_F)
            stop = outside[0] if len(outside) else len(after)
            held = S(after.t[:stop], after.v[:stop])
            near = np.flatnonzero(np.abs(held.v - target) <= LEFT_F)
            if not len(near):
                continue
            t1 = float(held.t[near[-1]])
            if t1 - arrive < REST_MIN_S:
                last_end = max(last_end, t1)
                continue
            held = held.window(arrive, t1)
            first10 = held.window(arrive, arrive + 600.0)
            inner = held.window(arrive + 120.0, t1 - 60.0)
            if not len(inner):
                inner = held.window(arrive + 120.0, t1)
            if not len(inner):
                inner = held
            approach = mash.window(search, arrive)
            if above:
                far = np.flatnonzero(approach.v > target + 1.0)
            else:
                far = np.flatnonzero(approach.v < target - 1.0)
            last_far = float(approach.t[far[-1]]) if len(far) else arrive
            out.append({"target": target, "t0": arrive, "t1": t1, "minutes": (t1 - arrive) / 60.0,
                        "held": float(np.max(np.abs(inner.v - target))),
                        "over": max(0.0, float(first10.v.max()) - target) if len(first10) else 0.0,
                        "last_degree": (arrive - last_far) / 60.0})
            last_end = t1
        return out

    def steps_up(self, rests):
        mash = self.probe("mash")
        out = []
        for a, b in zip(rests, rests[1:]):
            if b["target"] <= a["target"]:
                continue
            seg = mash.window(a["t1"] - 30.0, b["t0"])
            keep = seg.v >= a["target"] - 0.5
            seg = S(seg.t[keep], seg.v[keep])
            if len(seg) < 3 or seg.t[-1] - seg.t[0] < 120:
                continue
            rate = mid_rate(seg)
            if rate is None:
                continue
            out.append({"from": a["target"], "to": b["target"], "rate": rate, "t0": float(seg.t[0]),
                        "t1": float(b["t0"]), "minutes": (b["t0"] - seg.t[0]) / 60.0})
        return out

    def boil_in(self, t_from, t_to):
        """The boil in t_from..t_to: ("done", figure), ("stopped", highest reading) or (None, None)."""
        boil = self.probe("boil")
        w = boil.window(t_from, t_to)
        if not len(w):
            return None, None
        hot = np.flatnonzero(w.v >= BOIL_AT_F)
        if len(hot):
            k = int(hot[0])
            if k > 0 and float(w.v[:k].min()) < BOIL_PAST_F:
                tc = float(w.t[k])
                before = w.window(t_from, tc)
                under = np.flatnonzero(before.v < BOIL_FROM_F)
                tb = float(before.t[under[-1]]) if len(under) else float(before.t[0])
                low = before.window(max(t_from, tb - 3 * 3600.0), tb)
                floor = float(low.v.min())
                at_floor = np.flatnonzero(low.v <= floor + 0.3)
                t0 = float(low.t[at_floor[-1]])
                seg = boil.window(t0, tc)
                return "done", {"rate": mid_rate(seg), "from": float(seg.v[0]), "minutes": (tc - t0) / 60.0,
                                "t0": t0, "t1": tc}
        if rising(bucket(w), HEAT_RATE_F, HEAT_RISE_F):
            return "stopped", float(w.v.max())
        return None, None

    def autos(self, t_from, t_to):
        """The AUTOs that ran in t_from..t_to with no recipe step running: [{"vessel", "target"}], each once, leaving
        out one whose target wasn't logged. With no step log (a core before step numbers were logged), every one
        counts."""
        steps = self.series("steps.active")
        out = []
        for vessel in VESSELS:
            auto = self.change(vessel, "auto")
            if not len(auto):
                continue
            target = self.change(vessel, "target")
            for a, z in on_intervals(auto, t_from - 600.0, t_to):
                if a < t_from - 600.0 or a > t_to:
                    continue
                if len(steps) and (steps.at(a + 1) or 0) > 0:
                    continue
                aim = None
                if len(target):
                    aim = target.at(a + 1)
                    if not aim:
                        later = target.window(a, z)
                        nz = later.v[later.v > 0]
                        aim = float(nz[0]) if len(nz) else None
                if aim is None or aim <= 0:
                    continue
                entry = {"vessel": vessel, "target": _r(from_f(aim, self.unit), 1)}
                if entry not in out:
                    out.append(entry)
        return out

    def brewstart_record(self, t0, t1):
        """BrewStart's record of a heat-up (its B2 rows from 30 minutes before t0 to t1), or None."""
        rec = {}
        for key in ("planned_min", "ready_by", "litres", "watts", "efficiency"):
            s = self.series("brewstart." + key).window(t0 - 1800.0, t1)
            if len(s):
                rec[key] = float(s.v[-1])
        ready = self.series("brewstart.ready").window(t0, t1 + 3600.0)
        if len(ready):
            rec["ready_at"] = float(ready.t[0])
        return rec or None

    def hlt_cell(self, t_from, t_to):
        ups = [h for h in self.heatups() if t_from - 60 <= h["t0"] <= t_to]
        if not ups:
            return {"none": "not_heated"}, None
        good = [h for h in ups if h["alone"] and h["seen"] and h["to"] - h["from"] >= HEAT_RISE_F
                and (h["to"] - h["from"]) / max((h["t1"] - h["t0"]) / 60.0, 1e-9) >= HEAT_RATE_F]
        if not good:
            return {"none": "pump_on" if any(not h["alone"] for h in ups) else "not_heated"}, None
        h = good[0]
        rec = self.brewstart_record(h["t0"], h["t1"])
        litres = rec.get("litres") if rec else None
        watts = rec.get("watts") if rec else None
        if not (litres and watts) and self.hlt_sim:
            litres, watts = self.hlt_sim.get("litres"), self.hlt_sim.get("watts")
        stretch = {"t0": _r(h["t0"], 1), "t1": _r(h["t1"], 1)}
        if not litres or not watts or litres <= 0 or watts <= 0:
            return {"none": "no_water"}, stretch
        rise_k = (h["to"] - h["from"]) * 5.0 / 9.0
        seconds = h["t1"] - h["t0"]
        joules = litres * WATER_KG_PER_L * WATER_J_PER_KG_K * rise_k
        eff = joules / (watts * seconds)
        rate = mid_rate(self.probe("hlt").window(h["t0"], h["t1"]))
        if rec and rec.get("planned_min") is not None:
            planned, by = rec["planned_min"], "brewstart"
        else:
            planned, by = joules / (watts * BUILT_IN_EFFICIENCY) / 60.0, "worked_out"
        ready_by = rec.get("ready_by") if rec else None
        early = (ready_by - rec["ready_at"]) / 60.0 if rec and ready_by and rec.get("ready_at") else None
        volume = litres if self.unit == "C" else litres / L_PER_GAL
        u = self.unit
        return {"eff": _r(eff, 3), "rate": _r(delta_from_f(rate, u), 2), "from": _r(from_f(h["from"], u), 1),
                "to": _r(from_f(h["to"], u), 1), "minutes": _r(seconds / 60.0, 1), "volume": _r(volume, 2),
                "watts": _r(watts, 0), "t0": _r(h["t0"], 1), "t1": _r(h["t1"], 1), "planned": _r(planned, 1),
                "planned_by": by, "ready_by": _r(ready_by, 1), "early": _r(early, 1)}, stretch

    def brew(self, t_from, t_to, start=None, end=None):
        """One brew's own fields and its hlt, mash and boil cells, from the stretches in t_from..t_to."""
        u = self.unit
        rests = self.rests(t_from, t_to)
        ups = self.steps_up(rests)
        mash = self.probe("mash").window(t_from, t_to)
        auto = self.autos(t_from, t_to)
        if rests:
            mash_cell = {"rests": [{"target": _r(from_f(r["target"], u), 1), "t0": _r(r["t0"], 1),
                                    "t1": _r(r["t1"], 1), "minutes": _r(r["minutes"], 1),
                                    "held": _r(delta_from_f(r["held"], u), 2),
                                    "over": _r(delta_from_f(r["over"], u), 2),
                                    "last_degree": _r(r["last_degree"], 1)} for r in rests],
                         "steps_up": [{"from": _r(from_f(s["from"], u), 1), "to": _r(from_f(s["to"], u), 1),
                                       "rate": _r(delta_from_f(s["rate"], u), 2), "t0": _r(s["t0"], 1),
                                       "t1": _r(s["t1"], 1), "minutes": _r(s["minutes"], 1)} for s in ups]}
            stopped = False
        else:
            # Stopped before its first rest: the mash taken past 120 F, or its AUTO run at a rest, with no rest held.
            mash_auto = any(a["vessel"] == "mash" for a in auto) or any(
                z > t_from and a < t_to for a, z in on_intervals(self.change("mash", "auto"), t_from, t_to))
            heated = any(z >= t_from and a <= t_to for a, z in self.hot("mash", MASH_HOT_F))
            stopped = heated or bool(mash_auto and len(mash) and float(mash.v.max()) >= REST_LOWEST_F)
            mash_cell = {"none": "stopped" if stopped else "no_rest"}
        state, boil = self.boil_in(t_from, t_to)
        if state == "done" and boil["rate"] is not None:
            boil_cell = {"rate": _r(delta_from_f(boil["rate"], u), 2), "from": _r(from_f(boil["from"], u), 1),
                         "minutes": _r(boil["minutes"], 1), "t0": _r(boil["t0"], 1), "t1": _r(boil["t1"], 1)}
            boil_at = None
        elif state == "stopped":
            boil_cell = {"none": "stopped", "at": _r(from_f(boil, u), 1)}
            boil_at = boil_cell["at"]
            boil = None
        else:
            boil_cell, boil_at, state, boil = {"none": "no_boil"}, None, None, None
        hlt_cell, hlt_stretch = self.hlt_cell(t_from, t_to)
        windows = {}
        if rests:
            first = rests[0]
            climb = self.probe("mash").window(first["t0"] - 3 * 3600.0, first["t0"])
            below = np.flatnonzero(climb.v < first["target"] - 25.0)
            within = float(climb.t[below[-1]]) if len(below) else first["t0"] - 3 * 3600.0
            windows["mash"] = [_r(max(within, first["t0"] - 900.0), 1), _r(rests[-1]["t1"] + 720.0, 1)]
        return {
            "start": _r(start if start is not None else t_from, 1), "end": _r(end if end is not None else t_to, 1),
            "rests": [_r(from_f(r["target"], u), 1) for r in rests],
            "boil": state, "boil_at": boil_at, "auto": auto, "stopped_before_rest": stopped,
            "cells": {"hlt": hlt_cell, "mash": mash_cell, "boil": boil_cell},
            "stretches": {"hlt": [hlt_stretch] if hlt_stretch and "eff" in hlt_cell else [],
                          "mash": [{"t0": _r(r["t0"], 1), "t1": _r(r["t1"], 1)} for r in rests],
                          "boil": [{"t0": _r(boil["t0"], 1), "t1": _r(boil["t1"], 1)}] if boil else []},
            "windows": windows,
        }

    def stretches(self):
        """Every stretch in the logs: [(t0, t1, kind)], kind one of "heat_alone" (the HLT heated with the wort pump
        off), "heat_pump", "rest", "boil" (taken past 200 F from below), "mash_hot" (taken past 120 F from below) and
        "warm" (any probe warming)."""
        if hasattr(self, "_stretches"):
            return self._stretches
        out = []
        for h in self.heatups():
            out.append((h["t0"], max(h["t1"], h["rise_end"]), "heat_alone" if h["alone"] else "heat_pump"))
        mash_b = bucket(self.probe("mash"))
        for p0, p1, _ in self.plateaus():
            out.append((rise_start(mash_b, p0), p1, "rest"))
        for a, z in self.boil_climbs():
            out.append((a, z, "boil"))
        for a, z in self.hot("mash", MASH_HOT_F):
            out.append((a, z, "mash_hot"))
        for a, z in self.warming():
            out.append((a, z, "warm"))
        self._stretches = sorted(out)
        return self._stretches

    @staticmethod
    def counts(kinds):
        """Whether stretches of these kinds make a brew: the HLT heated by itself, the boil kettle taken past 200 F,
        or the mash past 120 F with a rest held or the HLT heating it."""
        return bool(kinds & {"heat_alone", "boil"}) or ("mash_hot" in kinds and bool(kinds & {"rest", "heat_pump"}))

    def found_brews(self):
        """The brews found in the logs: [(start, end)], by the rules in the module's docstring."""
        groups = []
        for t0, t1, kind in self.stretches():
            if groups and t0 - groups[-1][1] < BREW_GAP_S:
                g = groups[-1]
                groups[-1] = (g[0], max(g[1], t1), g[2] | {kind})
            else:
                groups.append((t0, t1, {kind}))
        return [(a, z) for a, z, kinds in groups if self.counts(kinds)]

    def start_of(self, t_from, t_to):
        """When heating began in t_from..t_to: the earliest start of a stretch there, or t_from with none."""
        firsts = [t0 for t0, t1, _ in self.stretches() if t1 >= t_from and t0 <= t_to]
        return max(t_from, min(firsts)) if firsts else t_from

    def landed(self, t):
        """The mash probe's average over minutes 5 to 15 after the malt went in at `t`, in F, or None."""
        w = self.probe("mash").window(t + 300.0, t + 900.0)
        return float(w.v.mean()) if len(w) else None


def analyse(folder, params, days=(), landings=(), cache=None):
    """The brews in one folder of logs.

    `days`: BrewScribe's brew days, [{"id", "start", "end", "probes": [{"key", "vessel", "t"}]}], times on this
    CraftBeerPi's clock. A day whose window has readings here is a brew here; `probes` asks for a probe's reading
    at a time (for a volume's temperature). `landings`: [{"t"}], when the malt went in (S1's record).

    Returns {"span", "brews": [...], "days": {day id: brew}}: each brew with "start", "end", "rests", "boil",
    "boil_at", "auto", "stopped_before_rest", "cells" (hlt, mash, boil), "stretches", and "landed" ({t: mash
    average}) for the landings in it; each day's also has "probes" ({key: reading in the brewery's unit})."""
    a = Analysis(folder, params, cache)
    span = a.span()
    out = {"span": span, "brews": [], "days": {}}
    if span is None:
        return out
    claimed = []
    for day in days:
        d0, d1 = float(day["start"]), float(day["end"])
        if d1 < span[0] or d0 > span[1]:
            continue
        brew = a.brew(d0, d1, start=a.start_of(d0, d1), end=d1)
        brew["probes"] = {}
        for p in day.get("probes") or []:
            reading = a.probe(p["vessel"]).near(float(p["t"]), within=600.0)
            brew["probes"][p["key"]] = _r(from_f(reading, a.unit), 2) if reading is not None else None
        brew["heated"] = _heated(a, d0, d1)
        out["days"][day["id"]] = brew
        claimed.append((d0, d1))
    for t0, t1 in a.found_brews():
        if any(t0 <= d1 and t1 >= d0 for d0, d1 in claimed):
            continue
        start = a.start_of(t0, t1)
        out["brews"].append(a.brew(start, t1, start=start, end=t1))
    for brew in list(out["brews"]) + list(out["days"].values()):
        brew["landed"] = {}
        for landing in landings:
            t = float(landing["t"])
            if brew["start"] <= t <= brew["end"]:
                value = a.landed(t)
                brew["landed"][repr(t)] = _r(from_f(value, a.unit), 2) if value is not None else None
    return out


def _heated(a, t0, t1):
    """How much heating a day's window holds here, in seconds of stretches: what picks the folder whose logs a
    day is in when more than one has readings then."""
    total = 0.0
    for s0, s1, _ in a.stretches():
        total += max(0.0, min(s1, t1) - max(s0, t0))
    return total
