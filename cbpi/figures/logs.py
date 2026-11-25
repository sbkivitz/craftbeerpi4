"""Reading CraftBeerPi's logs for figures: every reading of one log, as seconds on CraftBeerPi's clock and values.

A log is `sensor_<name>.log`, rotated to `.log.1` (the newest) up to `.log.<SENSOR_LOG_BACKUP_COUNT>` (the oldest), one
"YYYY-MM-DD HH:MM:SS.fff,value" row a reading, stamped with CraftBeerPi's clock as local time (LogController._emit).
The probes log as `sensor_<sensor id>.log`; the brew-day graphs' change logs as `sensor_<kettle id>.target.log`,
`sensor_<kettle id>.auto.log` and `sensor_<actor id>.out.log`.

Where logs are: the live logs folder (source "now"), and each accelerated simulator run SimVessel moved into
`archive/<time>-simulation/` when it ended (source <that folder's name>). An archived run never changes, so what is
worked out from it is kept (worker.py).

Rows a power cut stamped with the wrong time are left out by the charts' own rule (cbpi.api.logclock): the readings
the rig took before NTP set its clock again, stamped up to hours early among older ones, would otherwise put a dip in a
rest that never happened.
"""

import glob
import hashlib
import io
import os
import time

import numpy as np
import pandas as pd

from cbpi.api import logclock

ARCHIVE = "archive"
SIMULATION_SUFFIX = "-simulation"
LIVE = "now"

_EMPTY = (np.zeros(0), np.zeros(0))
_EMPTY_FILE = (np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0))


def log_files(folder, name):
    """`name`'s log files in `folder`, oldest first: the highest number is the oldest."""
    prefix = "sensor_{}.log".format(name)
    found = []
    for path in glob.glob(os.path.join(glob.escape(folder), glob.escape(prefix) + "*")):
        suffix = os.path.basename(path)[len(prefix):]
        if suffix == "":
            found.append((0, path))
        elif suffix.startswith(".") and suffix[1:].isdigit():
            found.append((int(suffix[1:]), path))
    return [path for _, path in sorted(found, reverse=True)]


def sources(logs_folder):
    """[(source, folder)]: the live logs ("now"), then each archived simulator run, oldest name first."""
    found = [(LIVE, logs_folder)]
    archive = os.path.join(logs_folder, ARCHIVE)
    if os.path.isdir(archive):
        for name in sorted(os.listdir(archive)):
            path = os.path.join(archive, name)
            if name.endswith(SIMULATION_SUFFIX) and os.path.isdir(path):
                found.append((name, path))
    return found


def source_folder(logs_folder, source):
    """The folder of a source named as sources() names it, or None for one that isn't there."""
    if source == LIVE:
        return logs_folder
    if not isinstance(source, str) or not source.endswith(SIMULATION_SUFFIX) or os.sep in source or "/" in source \
            or source.startswith("."):
        return None
    path = os.path.join(logs_folder, ARCHIVE, source)
    return path if os.path.isdir(path) else None


def fingerprint(folder, names):
    """What the logs of `names` in `folder` are now: each file's name, size and time, as one short text. It changes
    whenever a reading is written or a log rotates. One look at the folder, however many names."""
    wanted = {"sensor_%s.log" % n for n in names}
    parts = []
    try:
        with os.scandir(folder) as found:
            for entry in found:
                cut = entry.name.rfind(".log") + 4
                suffix = entry.name[cut:]
                if cut < 4 or entry.name[:cut] not in wanted:
                    continue
                if suffix and not (suffix.startswith(".") and suffix[1:].isdigit()):
                    continue
                try:
                    # Not entry.stat(): on Windows a folder's listing lags behind a file another process holds open.
                    st = os.stat(entry.path)
                except OSError:
                    continue
                parts.append("%s|%d|%d" % (entry.name, st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    return hashlib.sha1("\n".join(sorted(parts)).encode("utf-8")).hexdigest()


def _local_seconds(naive):
    """Seconds since the epoch for local times given as seconds since the epoch read as UTC, daylight saving and
    all: worked out once for each hour the readings fall in."""
    if not len(naive):
        return naive
    hours = np.floor(naive / 3600.0).astype(np.int64)
    unique, inverse = np.unique(hours, return_inverse=True)
    offsets = np.array([h * 3600 - time.mktime(time.gmtime(int(h) * 3600)[:8] + (-1,)) for h in unique.tolist()],
                       dtype=float)
    return naive - offsets[inverse]


def _parse(raw):
    """(stamps, times, values, moves) from one log file's bytes, in the order written: the stamps as written (seconds,
    read as UTC), the times they mean, the values, NaN for one that can't be read - its stamp still says something
    about the clock - and, for a row saying CraftBeerPi's clock moved there (cbpi.api.logclock.MOVED), how far, NaN for
    the others. Rows whose time can't be read are left out, as are the NUL bytes a power cut leaves where the file had
    grown but the data never reached the card."""
    raw = raw.replace(b"\x00", b"")
    if not raw.strip():
        return _EMPTY_FILE
    df = pd.read_csv(io.BytesIO(raw), names=["t", "v"], header=None, dtype=str, on_bad_lines="skip",
                     skip_blank_lines=True, engine="c")
    if df.empty:
        return _EMPTY_FILE
    stamps = pd.to_datetime(df["t"], format="ISO8601", errors="coerce")
    values = pd.to_numeric(df["v"], errors="coerce").to_numpy(dtype=float)
    if logclock.MOVED_BYTES in raw:
        moves = pd.to_numeric(df["v"].map(logclock.moved_by), errors="coerce").to_numpy(dtype=float)
    else:
        moves = np.full(len(values), np.nan)
    ok = (~stamps.isna()).to_numpy()
    if not ok.any():
        return _EMPTY_FILE
    naive = stamps[ok].dt.as_unit("ns").to_numpy().astype(np.int64) / 1e9
    return naive, _local_seconds(naive), values[ok], moves[ok]


def read_file(path, cache=None):
    """One log file's rows in the order written, as _parse gives them. `cache`, a dict, keeps files already read by
    their size and time: a rotated file keeps both when it's renamed, so it isn't read again."""
    try:
        st = os.stat(path)
    except OSError:
        return _EMPTY_FILE
    key = (os.path.basename(path).split(".log", 1)[0], st.st_size, st.st_mtime_ns)
    if cache is not None and key in cache:
        return cache[key]
    try:
        with open(path, "rb") as f:
            raw = f.read()
        found = _parse(raw)
    except (OSError, ValueError, pd.errors.ParserError):
        found = _EMPTY_FILE
    if cache is not None:
        cache[key] = found
    return found


def read_series(folder, name, cache=None):
    """Every reading of `name`'s logs in `folder`, sorted by time: (times, values) as numpy arrays. Each on the clock
    as it is now: moved by every move of CraftBeerPi's clock written after it (cbpi.api.logclock.moved_after), as
    the charts read them. The rows a clock that went back and was put right stamped are left out, judged in the order
    the files were written, as the charts judge them; then the values that can't be read."""
    parts = [read_file(path, cache) for path in log_files(folder, name)]
    parts = [p for p in parts if len(p[0])]
    if not parts:
        return _EMPTY
    stamps = np.concatenate([p[0] for p in parts])
    t = np.concatenate([p[1] for p in parts])
    v = np.concatenate([p[2] for p in parts])
    moves = np.concatenate([p[3] for p in parts])
    if np.isfinite(moves).any():
        t = t + logclock.moved_after(moves)
        stamps = t
    keep = ~logclock.misstamped(stamps) & np.isfinite(v)
    t, v = t[keep], v[keep]
    order = np.argsort(t, kind="stable")
    return t[order], v[order]


def value_range(folder, name):
    """(lowest, highest) of `name`'s readings in `folder`, reading only the values, or None with none. Quicker than
    read_series by half: what tells an archived run where nothing was heated from one worth reading."""
    lo, hi = None, None
    for path in log_files(folder, name):
        try:
            with open(path, "rb") as f:
                raw = f.read().replace(b"\x00", b"")
            if not raw.strip():
                continue
            v = pd.read_csv(io.BytesIO(raw), names=["t", "v"], header=None, usecols=[1], dtype=str,
                            on_bad_lines="skip", engine="c")["v"]
            v = pd.to_numeric(v, errors="coerce").to_numpy(dtype=float)
        except (OSError, ValueError, pd.errors.ParserError):
            continue
        v = v[np.isfinite(v)]
        if not len(v):
            continue
        lo = float(v.min()) if lo is None else min(lo, float(v.min()))
        hi = float(v.max()) if hi is None else max(hi, float(v.max()))
    return None if lo is None else (lo, hi)
