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
"""

import numpy as np

__all__ = ["BACK_TOLERANCE_S", "FIX_WITHIN_S", "misstamped"]

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
