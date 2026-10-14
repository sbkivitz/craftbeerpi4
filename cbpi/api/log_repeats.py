"""Keep one fault from flooding the log it will be diagnosed from.

Control loops tick once a second, and a fault that lasts - an actor that will
not switch off, a sensor that is gone - logged the same line on every tick:
"Actor pump reports still ON after being switched off", eighty-six thousand
times a day. The log rotates at 1 MB, four files deep, so an overnight fault
pushed its own beginning out of the log before anyone read it (DEFECTS F40).

RepeatFilter lets the first copy of a line through at once and then at most
one identical copy per `window` seconds, saying how many were held back.
Identical means the same place in the code, the same level and the same text:
a different actor, sensor or value is a different line and goes through.
"""

import logging
import time
from collections import OrderedDict

REPEAT_WINDOW_SEC = 300


class RepeatFilter(logging.Filter):
    """A handler filter. One instance may serve several handlers: each record is judged once."""

    def __init__(self, window=REPEAT_WINDOW_SEC, remembered=1000, clock=time.monotonic):
        super().__init__()
        self.window = window
        self.remembered = remembered
        self._clock = clock
        self._seen = OrderedDict()  # key -> [when last let through, held back since]

    def filter(self, record):
        verdict = getattr(record, "_repeat_verdict", None)
        if verdict is not None:
            return verdict
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - logging reports its own formatting errors
            return True
        key = (record.pathname, record.lineno, record.levelno, message)
        now = self._clock()
        seen = self._seen.get(key)
        if seen is not None and now - seen[0] < self.window:
            seen[1] += 1
            verdict = False
        else:
            if seen is not None and seen[1]:
                record.msg = "%s [and %d more like it in the last %.0fs]"
                record.args = (message, seen[1], now - seen[0])
            self._seen[key] = [now, 0]
            self._seen.move_to_end(key)
            while len(self._seen) > self.remembered:
                self._seen.popitem(last=False)
            verdict = True
        record._repeat_verdict = verdict
        return verdict


def install(logger=None, window=REPEAT_WINDOW_SEC):
    """One RepeatFilter on every handler of `logger` (the root logger by default)."""
    logger = logger or logging.getLogger()
    repeats = RepeatFilter(window)
    for handler in logger.handlers:
        if not any(isinstance(f, RepeatFilter) for f in handler.filters):
            handler.addFilter(repeats)
    return repeats
