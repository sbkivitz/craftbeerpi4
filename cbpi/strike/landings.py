"""Where each brew's strike water landed, kept for CraftBeerPi's Figures page.

S1: as the malt goes in (MashIn on the Mash Tun done, the brewer's Next), StrikeController appends the strike water
box's forecast for the strike in force to config/strike_landings.jsonl, one JSON object a line (append_landing).

S2: the worker works out, for each landing, what the model says the mash probe reads over WINDOW_MIN after that Next
with every number at its central guess but the share of the tun's steel the strike water warms (tun_share), at each
value of GRID, and the forecast's range as the malt goes in at each; and the mash probe's own average over the same
minutes, from CraftBeerPi's logs (probe_readings). fit() then finds the share that brings the model closest to the
landings counted, by least squares. StrikeController keeps it all in config/strike_fit.json.

The model's probe is compared with the logged probe, like with like. A mash probe at the HERMS coil's outlet reads the
wort leaving the coil, which PID_HERMS holds, not where the mash landed, so those landings don't count (why
"probe_at_coil"); nor does one whose model barely moves across the range (why "blind"). WINDOW_MIN after Next needs
checking against the rig's own readings.

Standard library only: the worker and Figures read it.
"""

import datetime
import glob
import json
import math
import os

LANDINGS_FILE = "strike_landings.jsonl"
FIT_FILE = "strike_fit.json"
VERSION = 1

#: The number fitted, and the values tried: its range, 45 % to 100 % of the tun's steel, in steps of 1 %.
FIT_GUESS = "tun_share"
GRID = tuple(round(0.45 + i * 0.01, 2) for i in range(56))

#: Minutes after Next - the malt in, as CraftBeerPi sees it - over which the probe's readings and the model's are
#: averaged. To be checked against the rig's own readings.
WINDOW_MIN = (5.0, 15.0)

#: Less than this between the model's averages at the range's ends, in F: the probe can't tell the steel.
BLIND_F = 0.25

#: Fewer readings than this in the window, or readings over less than half of it, give no landing.
MIN_READINGS = 5

#: Why a landing doesn't count, in the order a fit with none counted names them: its window isn't over yet; its mash
#: probe sits at the coil's outlet, where it reads the wort PID_HERMS heats, not the mash; the model couldn't be run
#: on it; the probe can't tell the steel (BLIND_F); no readings in its window.
WHY_ORDER = ("waiting", "probe_at_coil", "no_model", "blind", "no_readings")

ARCHIVE = "archive"
SIMULATION_SUFFIX = "-simulation"
_STAMP = "%Y-%m-%d %H:%M:%S"


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def to_f(value, unit):
    return value if unit == "F" else value * 1.8 + 32.0


def from_f(value, unit):
    return value if unit == "F" else (value - 32.0) / 1.8


def per_f(unit):
    """One F in `unit`'s degrees: for differences."""
    return 1.0 if unit == "F" else 1.0 / 1.8


# ---------------------------------------------------------------------------------------------------------------
# The landings file
# ---------------------------------------------------------------------------------------------------------------

def landing_id(record):
    """A landing's id, stable for as long as it is kept: its time and its recipe's first step."""
    record = record if isinstance(record, dict) else {}
    steps = record.get("steps")
    first = steps[0] if isinstance(steps, list) and steps and isinstance(steps[0], str) else ""
    t = record.get("t")
    return "%.3f|%s" % (t if _finite(t) else 0.0, first)


def usable_record(record):
    return (isinstance(record, dict) and _finite(record.get("t")) and isinstance(record.get("steps"), list)
            and record.get("unit") in ("F", "C") and _finite(record.get("strike"))
            and isinstance(record.get("job"), dict))


def probe_of(record):
    """Where the landing's mash probe sat: "coil" (the HERMS coil's outlet) or "tun" (the tun's)."""
    probe = ((record.get("job") or {}).get("brew") or {}).get("probe") or (record.get("numbers") or {}).get("probe")
    return probe if probe in ("coil", "tun") else "coil"


def read_landings(path):
    """Every landing kept, in the order kept. A line that can't be read - a power cut in the middle of writing it -
    is left out."""
    found = []
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return found
    for line in raw.replace(b"\x00", b"").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def append_landing(path, record):
    """One landing more, as one line, written in one go and pushed to the disk. A line a power cut left unfinished
    is ended first, so this one isn't lost with it."""
    line = (json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")
    with open(path, "ab+") as f:
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                line = b"\n" + line
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def file_key(path):
    """The landings file as it is now, {"size", "mtime_ns"}, or None when there is none."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


# ---------------------------------------------------------------------------------------------------------------
# The mash probe's readings
# ---------------------------------------------------------------------------------------------------------------

def window(record):
    """(start, end) of a landing's window, seconds on CraftBeerPi's clock."""
    t = float(record["t"])
    return t + WINDOW_MIN[0] * 60.0, t + WINDOW_MIN[1] * 60.0


def _log_files(folder, sensor):
    prefix = "sensor_{}.log".format(sensor)
    found = []
    for path in glob.glob(os.path.join(glob.escape(folder), glob.escape(prefix) + "*")):
        suffix = os.path.basename(path)[len(prefix):]
        if suffix == "":
            found.append((0, path))
        elif suffix.startswith(".") and suffix[1:].isdigit():
            found.append((int(suffix[1:]), path))
    return [path for _, path in sorted(found, reverse=True)]


def _folders(logs_folder):
    """The live logs, then each simulator run SimVessel archived (archive/<time>-simulation)."""
    found = [logs_folder]
    archive = os.path.join(logs_folder, ARCHIVE)
    if os.path.isdir(archive):
        for name in sorted(os.listdir(archive)):
            path = os.path.join(archive, name)
            if name.endswith(SIMULATION_SUFFIX) and os.path.isdir(path):
                found.append(path)
    return found


def _seconds(text):
    """Seconds on CraftBeerPi's clock from a log row's local time, "YYYY-MM-DD HH:MM:SS.fff"."""
    whole, _, fraction = text.partition(".")
    moment = datetime.datetime.strptime(whole, _STAMP)
    return moment.timestamp() + (float("0." + fraction) if fraction.isdigit() else 0.0)


def probe_readings(logs_folder, sensor, start, end):
    """[(seconds, value)] of `sensor`'s logged readings from `start` to `end` (seconds on CraftBeerPi's clock), from
    the live logs and every archived simulator run, sorted by time."""
    if not logs_folder or not sensor:
        return []
    # Rows are stamped as local time to the millisecond, which sorts as text in the order of time: only the rows
    # inside the window are parsed.
    low = datetime.datetime.fromtimestamp(start).strftime(_STAMP)
    high = datetime.datetime.fromtimestamp(end).strftime(_STAMP) + ".999"
    found = []
    for folder in _folders(logs_folder):
        for path in _log_files(folder, sensor):
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue
            for line in raw.replace(b"\x00", b"").decode("utf-8", "replace").splitlines():
                stamp, _, value = line.partition(",")
                stamp = stamp.strip()
                if not (low <= stamp <= high):
                    continue
                try:
                    reading = float(value)
                    when = _seconds(stamp)
                except ValueError:
                    continue
                if math.isfinite(reading) and start <= when <= end:
                    found.append((when, reading))
    found.sort()
    return found


def landed_from(readings, start, end):
    """The readings' average, or None with too few, or readings over less than half the window."""
    if len(readings) < MIN_READINGS or readings[-1][0] - readings[0][0] < (end - start) / 2.0:
        return None
    return sum(v for _, v in readings) / len(readings)


# ---------------------------------------------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------------------------------------------

def window_average(minutes, first, last):
    """The average over minutes `first` to `last` of a per-minute series (minute 0, 1, ...), read as straight lines
    between its minutes: what readings taken all through that window average to."""
    def at(m):
        i = min(max(int(math.floor(m)), 0), len(minutes) - 1)
        if i + 1 >= len(minutes):
            return minutes[-1]
        share = m - i
        return minutes[i] * (1.0 - share) + minutes[i + 1] * share

    if last <= first:
        return at(first)
    edges = [first] + [float(m) for m in range(int(math.floor(first)) + 1, int(math.ceil(last)))] + [last]
    total = sum((at(a) + at(b)) / 2.0 * (b - a) for a, b in zip(edges, edges[1:]))
    return total / (last - first)


def counted(entry):
    """A landing that counts in the fit: its probe's average known, and the probe able to tell the steel."""
    return (isinstance(entry, dict) and entry.get("why") is None and _finite(entry.get("landed"))
            and isinstance(entry.get("mid"), list) and len(entry["mid"]) == len(GRID))


def best(entries):
    """(the value of GRID that brings the model closest to the landings by least squares, the root of the mean
    squared distance there in F), or (None, None) with none counted."""
    entries = [e for e in entries if counted(e)]
    if not entries:
        return None, None
    sums = []
    for i in range(len(GRID)):
        total = 0.0
        for e in entries:
            total += ((e["mid"][i] - e["landed"]) / per_f(e["unit"])) ** 2
        sums.append(total)
    at = min(range(len(GRID)), key=lambda i: (sums[i], abs(GRID[i] - 0.75)))
    return GRID[at], math.sqrt(sums[at] / len(entries))


def fit(entries):
    """The share that brings the model closest to the landings `entries` (as strike_fit.json keeps them) by least
    squares, with what it says about each: {"value", "why", "brews", "rms_f", "edge", "landings": {id: {"fit_mid",
    "fit_lo", "fit_hi", "off"}}}. why, when there is no value, the first of WHY_ORDER any landing gives, else
    "no_landings" without any."""
    entries = [e for e in entries if isinstance(e, dict)]
    value, rms = best(entries)
    used = [e for e in entries if counted(e)]
    why = None
    if value is None:
        whys = {e.get("why") for e in entries}
        why = "no_landings" if not entries else next((w for w in WHY_ORDER if w in whys), "no_readings")
    each = {}
    if value is not None:
        i = GRID.index(value)
        for e in entries:
            if any(not isinstance(e.get(k), list) or len(e[k]) != len(GRID) for k in ("mid", "lo0", "hi0")):
                continue
            off = round(e["landed"] - e["mid"][i], 2) if _finite(e.get("landed")) else None
            each[e.get("id")] = dict(fit_mid=e["mid"][i], fit_lo=e["lo0"][i], fit_hi=e["hi0"][i], off=off)
    return dict(value=value, why=why, brews=len(used), rms_f=None if rms is None else round(rms, 3),
                edge=value is not None and value in (GRID[0], GRID[-1]), landings=each)
