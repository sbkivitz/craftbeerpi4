"""Which log rows a clock that went back, and was then put right, stamped: one rule for everything that reads the
logs - the charts (LogController._read_sensor_log) and Figures (cbpi.figures.logs).

A Raspberry Pi has no clock that runs without power. After a power cut it boots with the time it last saved, the
server starts and logs, and a minute or two later NTP sets the real time. The log then runs, say, 10:02:34, then
10:01:01 ... 10:03:09 for 129 rows, then 11:42:14: readings taken at about 11:40-11:42 are stamped 10:01-10:03, and a
chart would draw them two hours early, among the older readings - and a figure worked out from them would count a dip
that never happened.

So a step back of more than BACK_TOLERANCE_S, followed within FIX_WITHIN_S on the slow clock by a step forward that
makes up at least as much as was lost and lands past every earlier row, marks the rows between as misstamped. The
forward step is never smaller than the backward one: the slow clock is behind by the outage as well. A step is judged
after taking off the log's usual interval between readings, so a once-a-minute probe's ordinary 60 s does not pass for
the fix. When no such step comes - daylight saving ending, or NTP pulling a fast clock back - the clock that went back
was right, and every row is kept.

Left out rather than restamped: their true time is known only to about a reading interval, and a chart missing two
minutes is honest where one with two guessed minutes is not.

A simulator's clock is moved on purpose: a change of SIM_TIME_SCALE sets it back to the computer's time
(CraftBeerPi.brewing_clock), hours back after an accelerated run. Its rows are not misstamped, they are on the clock
as it was. So the move is written into each log as a row of its own, "<time>,clock moved <seconds>" (moved_row), its
time the moved clock's, and whatever reads the log moves every row written before it by as much (moved_after): the
readings already taken keep their places in the brew, and the ones after the move carry on from them, as one line.
The files keep what was written, the move's row included. Nothing on a rig moves its clock that way, so its logs have
no such row.
"""

import calendar
import math
import time

import numpy as np

__all__ = ["BACK_TOLERANCE_S", "FIX_WITHIN_S", "MOVED", "MOVED_BYTES", "misstamped", "moved_row", "moved_by",
           "moved_after", "shift_naive"]

#: The value of a log row that says CraftBeerPi's clock moved there, by the seconds after it: "clock moved -35400.123".
MOVED = "clock moved"
#: The same, as the bytes of a log file: a file without it has no move to read.
MOVED_BYTES = MOVED.encode("ascii")


def moved_row(stamp, seconds):
    """A log row saying the clock moved by `seconds` (negative: back), at `stamp`: the moved clock's time, as the logs
    write times."""
    return "{},{} {:+.3f}".format(stamp, MOVED, float(seconds))


def moved_by(value):
    """The seconds a log row's value says the clock moved by, or None for a reading."""
    text = str(value).strip()
    if not text.startswith(MOVED):
        return None
    try:
        seconds = float(text[len(MOVED):])
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


def moved_after(moves):
    """Seconds to add to each row's time to put it on the clock as it is now: the sum of the moves written after it.
    `moves` are the rows' moves in the order written, 0 or NaN for a reading."""
    m = np.nan_to_num(np.asarray(moves, dtype=float))
    if not len(m):
        return m
    total = np.cumsum(m)
    return total[-1] - total


def _offsets(seconds, written):
    """Each time's offset from UTC in seconds, worked out once for each hour the times fall in. `written`: the times
    are local times read as UTC, as the logs write them; else seconds since the epoch."""
    hours = np.floor(seconds / 3600.0).astype(np.int64)
    unique, inverse = np.unique(hours, return_inverse=True)
    if written:
        found = [h * 3600 - time.mktime(time.gmtime(int(h) * 3600)[:8] + (-1,)) for h in unique.tolist()]
    else:
        found = [calendar.timegm(time.localtime(int(h) * 3600)) - int(h) * 3600 for h in unique.tolist()]
    return np.asarray(found, dtype=float)[inverse]


def shift_naive(naive, shift):
    """Local times as the logs write them (seconds, read as UTC) moved by `shift` seconds of the clock, daylight saving
    and all."""
    naive = np.asarray(naive, dtype=float)
    if not len(naive):
        return naive
    moved = naive - _offsets(naive, True) + np.asarray(shift, dtype=float)
    return moved + _offsets(moved, False)

#: A step back in a log's timestamps bigger than this is the clock being changed, not two readings racing each other.
BACK_TOLERANCE_S = 2.0

#: How long a clock that went back may run before it is put right, measured on that clock. After a power cut, NTP sets
#: the rig's clock about two minutes after it boots.
FIX_WITHIN_S = 900.0


def misstamped(seconds, back_tolerance_s=BACK_TOLERANCE_S, fix_within_s=FIX_WITHIN_S):
    """Which rows to leave out: a boolean array, True for each row stamped by a clock that went back and was then put
    right. `seconds` are the rows' stamps, as seconds, in the order the rows were written."""
    s = np.asarray(seconds, dtype=float)
    n = len(s)
    drop = np.zeros(n, dtype=bool)
    if n < 2:
        return drop
    tol = back_tolerance_s

    def next_step_back(start, latest):
        if start >= n:
            return None
        run = np.maximum.accumulate(np.concatenate(([latest], s[start:])))
        behind = np.nonzero(run[:-1] - s[start:] > tol)[0]
        if behind.size == 0:
            return None
        k = int(behind[0])
        return start + k, float(run[k])

    found = next_step_back(0, -np.inf)
    if found is None:
        return drop  # nearly every log: the clock never went back
    steps = np.diff(s)
    positive = steps[steps > 0]
    usual = float(np.median(positive)) if positive.size else 0.0

    while found is not None:
        i, latest = found
        lost = latest - s[i]
        fixed_at = None
        for k in range(i + 1, n):
            if (s[k] - s[k - 1]) - usual >= lost - tol and s[k] > latest:
                fixed_at = k
                break
            if s[k] - s[i] > fix_within_s:
                break
        if fixed_at is None:
            # The clock that went back was right: keep its rows, and judge what follows by it.
            found = next_step_back(i + 1, s[i])
        else:
            drop[i:fixed_at] = True
            found = next_step_back(fixed_at, latest)
    return drop
