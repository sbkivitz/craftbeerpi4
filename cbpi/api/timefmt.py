"""Times of day as people read them, by the CLOCK_FORMAT setting.

CLOCK_FORMAT is "12-hour" - "11:15 PM", "7:00 AM", "11:15:40 PM", midnight "12:00 AM" and noon "12:00 PM" - or
"24-hour" - "23:15", "07:00", "23:15:40". A configuration without it, a blank one, or any other value counts as
24-hour, as on a core that has no such setting.

Only text written for people goes through here: notifications, a step's summary, messages for the screen. What is
saved, what an API takes or gives ("HH:MM"), log stamps, CSV columns and file names stay as they are.

    time_of_day(moment, cbpi)                      "11:15 PM" or "23:15"
    time_of_day(moment, cbpi, seconds=True)        "11:15:40 PM" or "23:15:40"
    day_and_time(moment, cbpi)                     "Sat 11:15 PM" or "Sat 23:15"
    day_and_time(moment, cbpi, day="%d.%m")        "22.11 11:15 PM" or "22.11 23:15"
    clock_format(cbpi)                             "12-hour" or "24-hour"

`moment` is a datetime, a time, seconds since the epoch (as clock.now() gives, shown in local time), or text in
the form "HH:MM" or "HH:MM:SS". The second argument is the setting's value or CraftBeerPi itself - anything with
config.get - to read it from. Nothing here reads a clock.

A plugin that must run on a core without this module too falls back to 24-hour:

    try:
        from cbpi.api.timefmt import time_of_day
    except ImportError:
        def time_of_day(moment, source=None, seconds=False):
            return moment.strftime("%H:%M:%S" if seconds else "%H:%M")
"""

import datetime as _dt

__all__ = [
    "SETTING",
    "TWELVE_HOUR",
    "TWENTY_FOUR_HOUR",
    "FORMATS",
    "DEFAULT",
    "DESCRIPTION",
    "clock_format",
    "twelve_hour",
    "time_of_day",
    "day_and_time",
]

#: The setting's name.
SETTING = "CLOCK_FORMAT"
TWELVE_HOUR = "12-hour"
TWENTY_FOUR_HOUR = "24-hour"
#: The setting's choices, in the order they are offered.
FORMATS = (TWELVE_HOUR, TWENTY_FOUR_HOUR)
#: What a configuration without the setting is given (ConfigUpdate). Reading a missing setting still gives 24-hour.
DEFAULT = TWELVE_HOUR
DESCRIPTION = "How times of day are shown: 12-hour (11:15 PM) or 24-hour (23:15)."


def clock_format(source=None):
    """ "12-hour" or "24-hour": what `source` says, 24-hour unless it is exactly "12-hour".

    `source` is the setting's value, or anything with config.get to read it from, such as CraftBeerPi. One whose
    config can't be read counts as without the setting. Never raises."""
    value = source
    if source is not None and not isinstance(source, str):
        try:
            value = source.config.get(SETTING, None)
        except Exception:  # noqa: BLE001 - a stand-in without settings, or settings not read yet
            value = None
    return TWELVE_HOUR if value == TWELVE_HOUR else TWENTY_FOUR_HOUR


def twelve_hour(source=None):
    """Whether `source` (as clock_format takes it) says 12-hour."""
    return clock_format(source) == TWELVE_HOUR


def _moment(moment):
    """A datetime or time for `moment`. Raises TypeError, ValueError, OverflowError or OSError (an epoch the platform
    can't show) for what isn't one."""
    if isinstance(moment, (_dt.datetime, _dt.time)):
        return moment
    if isinstance(moment, (int, float)) and not isinstance(moment, bool):
        return _dt.datetime.fromtimestamp(moment)
    if isinstance(moment, str):
        parts = moment.strip().split(":")
        if len(parts) in (2, 3) and all(p.isdigit() for p in parts):
            return _dt.time(*(int(p) for p in parts))
        raise ValueError("not a time of day in the form HH:MM or HH:MM:SS: %r" % moment)
    raise TypeError("not a time of day: %r" % (moment,))


def time_of_day(moment, source=None, seconds=False):
    """`moment`'s time of day, as `source`'s CLOCK_FORMAT shows it (see the module's docstring): "11:15 PM" or
    "23:15", with `seconds` "11:15:40 PM" or "23:15:40"."""
    t = _moment(moment)
    if not twelve_hour(source):
        return "%02d:%02d:%02d" % (t.hour, t.minute, t.second) if seconds else "%02d:%02d" % (t.hour, t.minute)
    text = "%d:%02d" % (t.hour % 12 or 12, t.minute)
    if seconds:
        text += ":%02d" % t.second
    return text + (" AM" if t.hour < 12 else " PM")


def day_and_time(moment, source=None, day="%a", seconds=False):
    """The day, as strftime's `day` writes it, then a space and the time of day: "Sat 11:15 PM" or "Sat 23:15";
    day="%Y-%m-%d" gives "2026-11-22 11:15 PM". `moment` must have a date: a datetime, or seconds since the
    epoch."""
    m = _moment(moment)
    if not isinstance(m, _dt.datetime):
        raise TypeError("a time of day alone has no day: %r" % (moment,))
    return "%s %s" % (m.strftime(day), time_of_day(m, source, seconds))
