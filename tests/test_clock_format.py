"""Times of day by the CLOCK_FORMAT setting (cbpi.api.timefmt).

- CLOCK_FORMAT is a setting: "12-hour" on a configuration without it, a select of "12-hour" and "24-hour" with its
  description refreshed with each version, a saved value kept.
- 12-hour: "11:15 PM", "7:00 AM", "11:15:40 PM", midnight "12:00 AM", noon "12:00 PM". 24-hour exactly as before:
  "23:15", "07:00", "23:15:40". Missing or any other value counts as 24-hour.
- What CraftBeerPi writes for people follows it: a step's "Estimated completion" notification, the cooldown step's
  summary, the times a step may carry on by itself until, the notification list's stamps, an HTTP or MQTT sensor's
  timeout warning.

Each test fails on its own on a core before CLOCK_FORMAT (4.7.7.post59).
"""

import asyncio
import time
import types
from datetime import datetime, time as day_time

import pytest

import cbpi.controller.notification_controller as notification_controller
from cbpi.api import clock
from cbpi.api.dataclasses import Props
from cbpi.api.timer import Timer
from cbpi.controller.notification_controller import NotificationController
from cbpi.controller.step_controller import StepController
from cbpi.extension.ConfigUpdate import ConfigUpdate
from cbpi.extension.httpsensor import HTTPSensor
from cbpi.extension.mashstep import BoilStep, CooldownStep, MashStep, SpargeStep
from cbpi.extension.mqtt_sensor import MQTTSensor, MQTTSensorOffset

try:
    from cbpi.api import timefmt
except ImportError:  # a core before CLOCK_FORMAT: each test that needs it fails on its own
    timefmt = None

TWELVE = {"CLOCK_FORMAT": "12-hour"}
TWENTY_FOUR = {"CLOCK_FORMAT": "24-hour"}
DESCRIPTION = "How times of day are shown: 12-hour (11:15 PM) or 24-hour (23:15)."


class Config:
    """The settings, recording what ConfigUpdate adds."""

    def __init__(self, values=None):
        self.values, self.added = dict(values or {}), {}
        self.cache = {name: types.SimpleNamespace(value=value) for name, value in self.values.items()}

    def get(self, name, default=None):
        value = self.values.get(name)
        return default if value is None or value == "" else value

    async def add(self, name, value, type=None, description="", source="craftbeerpi", options=None):
        self.values[name] = value
        self.cache[name] = types.SimpleNamespace(value=value)
        self.added[name] = dict(value=value, type=getattr(type, "value", type), description=description,
                                source=source, options=options)


def server(values=None):
    """A CraftBeerPi stand-in with these settings, recording what it is told."""
    cbpi = types.SimpleNamespace(config=Config(values), notes=[], kettle=object(), ws=types.SimpleNamespace(
        send=lambda *a, **k: None), push_update=lambda *a, **k: None)
    cbpi.notify = lambda title, message, type=None, action=None, **k: cbpi.notes.append((title, message))
    cbpi.step = types.SimpleNamespace(push_udpate=lambda: None, save=_nothing)
    return cbpi


async def _nothing(*a, **k):
    return None


def local(*args):
    return datetime(*args).timestamp()


def _id(value):
    """Test ids without spaces, so a failing test's id is one word: the setting, or a time without its space."""
    if isinstance(value, dict):
        return value.get("CLOCK_FORMAT", "missing")
    if isinstance(value, str):
        return value.replace(" ", "")
    return None


# -- the helper ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("hms, twelve, twenty_four", [
    ((23, 15, 40), "11:15 PM", "23:15"),
    ((7, 0, 0), "7:00 AM", "07:00"),
    ((0, 0, 0), "12:00 AM", "00:00"),       # midnight
    ((12, 0, 0), "12:00 PM", "12:00"),      # noon
    ((0, 5, 9), "12:05 AM", "00:05"),
    ((12, 59, 0), "12:59 PM", "12:59"),
    ((13, 0, 0), "1:00 PM", "13:00"),
    ((11, 59, 59), "11:59 AM", "11:59"),
], ids=_id)
def test_a_time_of_day_in_each_format(hms, twelve, twenty_four):
    moment = datetime(2026, 11, 22, *hms)
    assert timefmt.time_of_day(moment, "12-hour") == twelve
    assert timefmt.time_of_day(moment, "24-hour") == twenty_four == moment.strftime("%H:%M")


@pytest.mark.parametrize("hms, twelve", [((23, 15, 40), "11:15:40 PM"), ((0, 0, 0), "12:00:00 AM"),
                                         ((12, 0, 5), "12:00:05 PM"), ((7, 3, 9), "7:03:09 AM")], ids=_id)
def test_with_seconds(hms, twelve):
    moment = datetime(2026, 11, 22, *hms)
    assert timefmt.time_of_day(moment, "12-hour", seconds=True) == twelve
    assert timefmt.time_of_day(moment, "24-hour", seconds=True) == moment.strftime("%H:%M:%S")


@pytest.mark.parametrize("value", [None, "", "24-hour", "12h", "12-Hour", " 12-hour", "12", 12, True],
                         ids=lambda v: repr(v).replace(" ", "_"))
def test_missing_or_any_other_value_is_24_hour(value):
    cbpi = server({} if value is None else {"CLOCK_FORMAT": value})
    assert timefmt.clock_format(cbpi) == "24-hour"
    assert timefmt.time_of_day(datetime(2026, 11, 22, 23, 15), cbpi) == "23:15"


def test_the_setting_is_read_from_craftbeerpi_or_given_as_its_value():
    late = datetime(2026, 11, 22, 23, 15)
    assert timefmt.time_of_day(late, server(TWELVE)) == "11:15 PM"
    assert timefmt.time_of_day(late, server(TWENTY_FOUR)) == "23:15"
    assert timefmt.time_of_day(late) == "23:15"
    # a stand-in without settings, or one whose settings can't be read
    assert timefmt.time_of_day(late, types.SimpleNamespace()) == "23:15"
    broken = types.SimpleNamespace(config=types.SimpleNamespace(get=lambda *a: 1 / 0))
    assert timefmt.time_of_day(late, broken) == "23:15"


def test_an_epoch_a_time_and_hh_mm_text():
    at = local(2026, 11, 22, 23, 15, 40)
    assert timefmt.time_of_day(at, "12-hour") == "11:15 PM"
    assert timefmt.time_of_day(at, "24-hour", seconds=True) == "23:15:40"
    assert timefmt.time_of_day(day_time(0, 30), "12-hour") == "12:30 AM"
    assert timefmt.time_of_day("07:00", "12-hour") == "7:00 AM"
    assert timefmt.time_of_day("12:00", "12-hour") == "12:00 PM"
    assert timefmt.time_of_day("23:15:40", "12-hour", seconds=True) == "11:15:40 PM"
    assert timefmt.time_of_day("07:00", "24-hour") == "07:00"
    for bad in ("7 pm", "25:00", "", None):
        with pytest.raises((TypeError, ValueError)):
            timefmt.time_of_day(bad, "12-hour")


def test_a_day_and_a_time():
    moment = datetime(2026, 11, 21, 7, 0)
    assert timefmt.day_and_time(moment, "12-hour") == moment.strftime("%a") + " 7:00 AM"
    assert timefmt.day_and_time(moment, "24-hour") == moment.strftime("%a %H:%M")
    assert timefmt.day_and_time(moment, "12-hour", day="%d.%m") == "21.11 7:00 AM"
    assert timefmt.day_and_time(moment, "24-hour", day="%d.%m") == "21.11 07:00"
    assert timefmt.day_and_time(moment.timestamp(), "12-hour", day="%Y-%m-%d", seconds=True) == "2026-11-21 7:00:00 AM"
    with pytest.raises(TypeError):
        timefmt.day_and_time("07:00", "12-hour")


# -- the setting -----------------------------------------------------------------------------------------------------

def configured(tmp_path, values):
    cbpi = types.SimpleNamespace(
        config=Config(values), static_config={"mqtt_offset": False},
        plugin=types.SimpleNamespace(load_plugin_list=_list),
        config_folder=types.SimpleNamespace(get_dashboard_path=lambda name: str(tmp_path / name),
                                            get_file_path=lambda name: str(tmp_path / name)))
    update = ConfigUpdate.__new__(ConfigUpdate)
    update.cbpi = cbpi
    asyncio.run(update.run())
    return cbpi.config


async def _list(*a, **k):
    return []


def test_a_configuration_without_it_gets_12_hour(tmp_path):
    added = configured(tmp_path, {"TEMP_UNIT": "F"}).added.get("CLOCK_FORMAT")
    assert added == dict(value="12-hour", type="select", description=DESCRIPTION, source="craftbeerpi",
                         options=[{"label": "12-hour", "value": "12-hour"}, {"label": "24-hour", "value": "24-hour"}])


def test_a_saved_value_is_kept_and_its_description_follows_the_code(tmp_path):
    added = configured(tmp_path, {"TEMP_UNIT": "F", "CLOCK_FORMAT": "24-hour",
                                  "CONFIG_STATUS": "4.7.7.post1"}).added.get("CLOCK_FORMAT")
    assert added is not None and added["value"] == "24-hour" and added["description"] == DESCRIPTION
    assert [o["value"] for o in added["options"]] == ["12-hour", "24-hour"]


# -- what CraftBeerPi writes for people ------------------------------------------------------------------------------

async def _timed(kind, cbpi):
    """A step of `kind` started and run until its timer, sixty minutes, has said when it ends."""
    if kind is SpargeStep:
        step = SpargeStep(cbpi, "s1", "Sparge", Props({"Timer": 60}), None)
        step.timer = Timer(3600)
        step.record = step._hlt_auto = _nothing
        await step._start_sparge()
        await step.timer.stop()
        return
    props, reading = ({"Timer": 60, "Temp": 65, "AutoMode": "No"}, 65) if kind is MashStep else (
        {"Timer": 60, "Temp": 100, "AutoMode": "No", "LidAlert": "No"}, 100)
    step = kind(cbpi, "s1", kind.__name__, Props(props), None)
    step.get_kettle = lambda _id: types.SimpleNamespace(target_temp=0)
    step.get_sensor_value = lambda _id: {"value": reading}
    await step.on_start()
    step.running = True
    task = asyncio.create_task(step.run())
    try:
        for _ in range(300):
            if any("Estimated completion" in m for _, m in cbpi.notes):
                return
            await asyncio.sleep(0.01)
    finally:
        step.running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await step.timer.stop()


def announced(kind, values, now):
    """What a step of `kind` says as its sixty-minute timer starts, with the wall clock at `now`."""
    cbpi = server(dict(values, CONFIRM_BEFORE_BOIL="No"))
    real = time.time
    time.time = lambda: now
    try:
        asyncio.run(_timed(kind, cbpi))
    finally:
        time.time = real
    return next((m for _, m in cbpi.notes if "Estimated completion" in m), cbpi.notes)


SAID = {MashStep: "Timer started. Estimated completion: ", BoilStep: "Timer started. Estimated completion: ",
        SpargeStep: "Sparge timer started. Estimated completion: "}


@pytest.mark.parametrize("kind", [MashStep, BoilStep, SpargeStep])
@pytest.mark.parametrize("values, start, said", [
    (TWELVE, (22, 15), "11:15 PM"), (TWENTY_FOUR, (22, 15), "23:15"), ({}, (22, 15), "23:15"),
    (TWELVE, (11, 0), "12:00 PM"), (TWENTY_FOUR, (11, 0), "12:00"),     # ending at noon
    (TWELVE, (23, 0), "12:00 AM"), (TWENTY_FOUR, (23, 0), "00:00"),     # and at midnight
], ids=_id)
def test_a_timer_names_its_end_in_each_format(kind, values, start, said):
    # The estimate is on the wall clock the brew day is watched by (mashstep._completion_time).
    assert announced(kind, values, local(2026, 11, 22, *start)) == SAID[kind] + said


def cooling(values, started, estimate=None):
    """A cooldown step's summary once it has run, started at `started`; with `estimate`, the time the wort was
    cooling 10 degrees in ten minutes from: its summary and what it said, once it has worked out its end."""
    async def run():
        cbpi = server(values)
        step = CooldownStep(cbpi, "c1", "Cool", Props({"Temp": 68, "Kettle": "k", "Sensor": "s"}), None)
        step.get_kettle = lambda _id: types.SimpleNamespace(target_temp=0)
        step.get_sensor_value = lambda _id: {"value": 160}
        await step.on_start()
        step.start_time = started
        if estimate is not None:
            # 180, 170 and 160 degrees ten minutes apart: 68 is reached 112 minutes after the first
            step.temp_array, step.time_array = [180, 170, 160], [estimate, estimate + 600, estimate + 1200]
            step.next_check = 0
        step.running = True
        task = asyncio.create_task(step.run())
        try:
            for _ in range(100):
                if step.summary.startswith("ECT" if estimate is not None else "Started"):
                    break
                await asyncio.sleep(0.01)
            return step.summary, [m for t, m in cbpi.notes if t == "Cooldown Step"]
        finally:
            step.running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(run())


@pytest.mark.parametrize("start, twelve, twenty_four", [((23, 15), "11:15 PM", "23:15"),
                                                        ((0, 0), "12:00 AM", "00:00"),
                                                        ((12, 0), "12:00 PM", "12:00")], ids=_id)
def test_the_cooldown_steps_summary_in_each_format(start, twelve, twenty_four):
    started = local(2026, 11, 22, *start)
    assert cooling(TWELVE, started)[0] == "Started: " + twelve
    assert cooling(TWENTY_FOUR, started)[0] == "Started: " + twenty_four
    assert cooling({}, started)[0] == "Started: " + twenty_four


def test_the_cooldown_steps_estimate_in_each_format():
    # 21:23:30 and 112 minutes: 23:15:30
    first = local(2026, 11, 22, 21, 23, 30)
    assert cooling(TWELVE, first, first) == ("ECT: 11:15 PM", ["Current: 160°, reaching 68.0° at 22.11 11:15 PM"])
    assert cooling(TWENTY_FOUR, first, first) == ("ECT: 23:15", ["Current: 160°, reaching 68.0° at 22.11 23:15"])
    assert cooling({}, first, first) == ("ECT: 23:15", ["Current: 160°, reaching 68.0° at 22.11 23:15"])

def test_until_when_a_step_may_carry_on_by_itself():
    steps = StepController.__new__(StepController)
    previous = clock.set_source(clock.VirtualTimeSource(start=local(2026, 11, 22, 21, 0)))
    try:
        tonight, saturday = local(2026, 11, 22, 23, 15), local(2026, 11, 28, 7, 0)
        day = datetime(2026, 11, 28).strftime("%a")
        steps.cbpi = server(TWELVE)
        assert (steps._clock_time(tonight), steps._clock_time(saturday)) == ("11:15 PM", day + " 7:00 AM")
        steps.cbpi = server(TWENTY_FOUR)
        assert (steps._clock_time(tonight), steps._clock_time(saturday)) == ("23:15", day + " 07:00")
        steps.cbpi = server({})
        assert (steps._clock_time(tonight), steps._clock_time(saturday)) == ("23:15", day + " 07:00")
        assert steps._clock_time("never") == "?"
    finally:
        clock.set_source(previous)


class At(datetime):
    """datetime, its now() a fixed moment."""
    moment = None

    @classmethod
    def now(cls, tz=None):
        return cls.moment


def listed(values, moment):
    """The notification list's newest line after one notification at `moment`."""
    notes = NotificationController.__new__(NotificationController)
    notes.cbpi = server(values)
    notes.callback_cache, notes.listener, notes.notifications = {}, {}, []
    notes.update_key, notes.sorting = "notificationupdate", False

    async def notify():
        notes.notify("Boil", "Add the hops")
        await asyncio.sleep(0)

    real, notification_controller.datetime = notification_controller.datetime, At
    At.moment = At(*moment)
    try:
        asyncio.run(notify())
    finally:
        notification_controller.datetime = real
    return notes.notifications[0][0]


def test_the_notification_lists_stamps_in_each_format():
    assert listed(TWELVE, (2026, 11, 22, 23, 15, 40)) == "2026-11-22 11:15:40 PM: Boil | Add the hops"
    assert listed(TWELVE, (2026, 11, 22, 0, 0, 5)) == "2026-11-22 12:00:05 AM: Boil | Add the hops"
    assert listed(TWENTY_FOUR, (2026, 11, 22, 23, 15, 40)) == "2026-11-22 23:15:40: Boil | Add the hops"
    assert listed({}, (2026, 11, 22, 7, 0, 0)) == "2026-11-22 07:00:00: Boil | Add the hops"


@pytest.mark.parametrize("kind", [HTTPSensor, MQTTSensor, MQTTSensorOffset])
def test_a_sensors_timeout_says_when_it_last_heard_in_each_format(kind):
    def says(values):
        sensor = kind.__new__(kind)
        sensor.cbpi = server(values)
        sensor.sensor = types.SimpleNamespace(name="Mash")
        sensor.lastdata = local(2026, 11, 22, 23, 15)
        asyncio.run(sensor.message())
        return sensor.cbpi.notes[0][1]
    assert says(TWELVE).endswith("Last data received: 11/22/26 11:15 PM")
    assert says(TWENTY_FOUR).endswith("Last data received: 11/22/26 23:15")
    assert says({}).endswith("Last data received: 11/22/26 23:15")
