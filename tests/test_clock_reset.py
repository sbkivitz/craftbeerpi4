"""A simulator's change of speed sets CraftBeerPi's clock back to the computer's time (cbpi.api.clock.reset,
CraftBeerPi.brewing_clock), and whatever keeps a time on the clock moves with it, so the brew carries on with as long
to go: a step's timer, the brew steps' kept times, the brew-day timers, the alarms' watches; BrewStart plans again;
a kettle logic that can't move its own times starts again; the logs written ahead are archived; every screen is
told, and a notification says it."""

import asyncio
import logging
import os
from datetime import datetime
from unittest import mock

import numpy as np
import pytest

from cbpi.api import clock
from cbpi.api.dataclasses import Kettle, Props, Step, StepState
from cbpi.api.step import CBPiStep, StepResult
from cbpi.api.timer import Timer
from cbpi.controller.kettle_controller import KettleController
from cbpi.controller.step_controller import StepController
from cbpi.controller.timers_controller import Timer as BrewDayTimer
from cbpi.controller.timers_controller import TimersController

# Looked up, not imported, so on a core without them each test fails on its own.
try:
    from cbpi.controller import clock_controller
except ImportError:  # pragma: no cover - a core before the reset
    clock_controller = None

DAY = 86400.0


class Manual(clock.TimeSource):
    """The computer's clock, moved by hand."""

    def __init__(self, now=1_795_400_000.0, mono=10_000.0):
        self._now, self._mono = float(now), float(mono)

    def now(self):
        return self._now

    def monotonic(self):
        return self._mono

    async def sleep(self, seconds):
        await asyncio.sleep(0)

    def advance(self, seconds):
        self._now += seconds
        self._mono += seconds


@pytest.fixture
def pc():
    """A simulator's clock at 30x on a computer whose clock moves only by hand; the clock as it was, after."""
    computer = Manual()
    previous = clock.set_source(clock.ScaledTimeSource(30, reference=computer))
    yield computer
    clock.set_source(previous)


def ahead_by_two_days(computer):
    """Run the 30x clock until it is two days ahead of the computer's."""
    computer.advance(2 * DAY / 29)
    assert clock.now() - computer.now() == pytest.approx(2 * DAY)


# --- the clock itself -------------------------------------------------------------------------------------------------

def test_reset_takes_now_back_to_the_computers_time_and_monotonic_carries_on(pc):
    ahead_by_two_days(pc)
    mono = clock.monotonic()
    moved = clock.reset(1)
    assert moved == pytest.approx(-2 * DAY)
    assert clock.now() == pytest.approx(pc.now())
    assert clock.monotonic() == pytest.approx(mono)
    assert clock.scale() == 1.0
    pc.advance(10)
    assert clock.now() == pytest.approx(pc.now())
    assert clock.monotonic() == pytest.approx(mono + 10)


def test_reset_runs_at_the_new_speed_from_the_computers_time(pc):
    ahead_by_two_days(pc)
    clock.reset(60)
    start, mono = pc.now(), clock.monotonic()
    pc.advance(10)
    assert clock.now() == pytest.approx(start + 600)
    assert clock.monotonic() == pytest.approx(mono + 600)


def test_reset_from_real_time_installs_a_scaled_clock_at_the_computers_time():
    previous = clock.set_source(clock.RealTimeSource())
    try:
        assert clock.reset(30) == 0.0
        assert isinstance(clock.get_source(), clock.ScaledTimeSource)
        assert clock.scale() == 30.0
        assert abs(clock.now() - clock.RealTimeSource().now()) < 1.0
    finally:
        clock.set_source(previous)


@pytest.mark.parametrize("value", ["abc", 0, -2, float("inf"), float("nan"), None])
def test_reset_to_a_speed_it_cannot_run_at_changes_nothing(pc, value):
    ahead_by_two_days(pc)
    before = clock.now()
    assert clock.reset(value) is None
    assert clock.scale() == 30.0
    assert clock.now() == pytest.approx(before)


# --- a step's timer ---------------------------------------------------------------------------------------------------

def test_a_counting_timer_keeps_the_time_it_had_left(pc):
    async def scenario():
        timer = Timer(1200)
        timer.start()
        await asyncio.sleep(0.01)
        pc.advance(20)  # 600 brewing seconds at 30x
        left = timer.seconds_left()
        ends = timer.state()["ends_at"]
        moved = clock.reset(1)
        Timer.clock_moved_all(moved)
        after = timer.seconds_left()
        state = timer.state()
        await asyncio.sleep(0.05)
        still = timer._task is not None and not timer._task.done()
        await timer.stop()
        return left, after, ends, moved, state, still

    left, after, ends, moved, state, still = asyncio.run(scenario())
    assert left == pytest.approx(600, abs=1)
    assert after == pytest.approx(left, abs=1)
    assert state["ends_at"] == pytest.approx(ends + moved)
    assert state["ends_at"] - clock.now() == pytest.approx(600, abs=1)
    assert still, "the timer ended at the jump"


def test_a_stopped_timer_still_has_what_it_had_left(pc):
    async def scenario():
        timer = Timer(1200)
        timer.start()
        await asyncio.sleep(0.01)
        pc.advance(20)
        await timer.stop()
        left = timer.seconds_left()
        Timer.clock_moved_all(clock.reset(1))
        return left, timer.seconds_left()

    left, after = asyncio.run(scenario())
    assert after == pytest.approx(left)


# --- the brew steps ---------------------------------------------------------------------------------------------------

class Plain(CBPiStep):
    async def run(self):
        while self.running:
            await asyncio.sleep(0.01)
        return StepResult.DONE


class BrewStartLike(CBPiStep):
    """A BrewStart as the core sees one: its type's name, its reset() and on_start()."""

    def __init__(self, *args):
        super().__init__(*args)
        self.calls = []

    async def reset(self):
        self.calls.append("reset")
        self.props["_brewstart_planned"] = ""

    async def on_start(self):
        self.calls.append("start")

    async def on_stop(self):
        self.calls.append("stop")

    async def run(self):
        while self.running:
            await asyncio.sleep(0.01)
        return StepResult.DONE


class BrewStartMovingItself(BrewStartLike):
    def clock_moved(self, seconds):
        self.calls.append(("moved", seconds))


def steps(*items):
    controller = StepController.__new__(StepController)
    controller.cbpi = mock.Mock()
    controller.profile = []
    controller.basic_data = {}
    for ident, kind, status, props in items:
        props = Props(props)
        instance = kind(controller.cbpi, ident, ident, props, controller.done) if kind else None
        controller.profile.append(Step(ident, ident, props=props, type=getattr(kind, "TYPE", None) or (
            "BrewStart" if kind in (BrewStartLike, BrewStartMovingItself) else "MashStep"), status=status,
            instance=instance))
    return controller


def test_the_steps_kept_times_move_with_the_clock():
    controller = steps(("mash", Plain, StepState.ACTIVE, {"_resume_until": 1000.0, "_started_at": 900.0}),
                       ("boil", Plain, StepState.INITIAL, {"_resume_until": "soon", "Timer": 60}))
    controller.basic_data = {"loaded_at": 800.0, "_mash_ph_reminder": {"confirmed_at": 950.0, "due_at": 1850.0}}
    controller.clock_moved(-100.0)
    mash, boil = controller.profile
    assert mash.props["_resume_until"] == 900.0
    assert mash.props["_started_at"] == 800.0
    assert boil.props["_resume_until"] == "soon"
    assert boil.props["Timer"] == 60
    assert controller.basic_data["loaded_at"] == 700.0
    assert controller.basic_data["_mash_ph_reminder"]["confirmed_at"] == 850.0
    assert controller.basic_data["_mash_ph_reminder"]["due_at"] == 1750.0


def test_a_steps_own_watches_move_with_the_clock():
    controller = steps(("mash", Plain, StepState.ACTIVE, {}))
    step = controller.profile[0].instance
    step._elapsed_saved_at = 5000.0
    step._stall_anchor_value, step._stall_anchor_at = 150.0, 4000.0
    controller.clock_moved(-1000.0)
    assert step._elapsed_saved_at == 4000.0
    assert step._stall_anchor_at == 3000.0


def test_the_boils_readings_move_with_the_clock():
    from cbpi.extension.mashstep import BoilStep
    boil = BoilStep(mock.Mock(), "boil", "Boil", Props({}), None)
    boil._elapsed_saved_at = 0.0
    boil.temptimes = np.array([5000.0, 5001.0])
    boil.clock_moved(-1000.0)
    assert list(boil.temptimes) == [4000.0, 4001.0]
    assert boil._elapsed_saved_at == 0.0


def test_brewstart_waiting_plans_again_from_its_settings():
    controller = steps(("bs", BrewStartLike, StepState.ACTIVE, {"_brewstart_planned": "x|2026-11-24T23:15:00"}))
    step = controller.profile[0]

    async def scenario():
        await step.instance.start()
        await asyncio.sleep(0.02)
        replanned = await controller.replan_after_clock_reset()
        await asyncio.sleep(0.02)
        running = step.instance.task is not None and not step.instance.task.done()
        await step.instance.stop()
        return replanned, running

    replanned, running = asyncio.run(scenario())
    assert replanned is True
    assert step.instance.calls[:4] == ["start", "stop", "reset", "start"]
    assert running
    assert step.status == StepState.ACTIVE
    assert step.props["_brewstart_planned"] == ""


def test_brewstart_not_running_forgets_its_plan_and_one_done_is_left_alone():
    controller = steps(("stopped", BrewStartLike, StepState.STOP, {"_brewstart_planned": "x|y"}),
                       ("done", BrewStartLike, StepState.DONE, {"_brewstart_planned": "x|y"}),
                       ("own", BrewStartMovingItself, StepState.STOP, {"_brewstart_planned": "x|y"}))
    asyncio.run(controller.replan_after_clock_reset())
    stopped, done, own = (s.instance for s in controller.profile)
    assert stopped.calls == ["reset"]
    assert done.calls == []
    assert own.calls == []


# --- the brew-day timers ----------------------------------------------------------------------------------------------

def test_brew_day_timers_keep_their_time_and_ran_out_at_moves(pc):
    timers = TimersController.__new__(TimersController)
    timers.cbpi = mock.Mock()
    timers.logger = logging.getLogger("test")
    timers.timers, timers._removed = [], {}
    rang = BrewDayTimer(2, "Yeast", 600)
    rang.state, rang.at, rang.rang = "done", clock.now() - 60, clock.monotonic() - 60
    ahead_by_two_days(pc)
    running = BrewDayTimer(1, "Hop stand", 600)
    running.state, running.deadline = "running", clock.monotonic() + 300
    timers.timers = [running, rang]
    left = running.left()
    moved = clock.reset(1)
    timers.clock_moved(moved)
    assert running.left() == pytest.approx(left)
    assert clock.now() - rang.at == pytest.approx(60 + 2 * DAY / 29 * 30, rel=1e-6)
    timers.cbpi.ws.send.assert_called()


# --- the alarms' watches ----------------------------------------------------------------------------------------------

def test_the_alarms_watches_move_with_the_clock():
    from cbpi.extension.byhand import HeatingByHand
    from cbpi.extension.notarget import NoTargetWarning
    byhand = HeatingByHand(mock.Mock(), watch=False)
    byhand._stretch = {"hlt": {"kettle": "k", "target": 150, "raised": set(), "since": 5000.0},
                       "bk": {"kettle": None, "target": None, "raised": set(), "since": None}}
    notarget = NoTargetWarning(mock.Mock(), watch=False)
    notarget._since = {"k": 5000.0}
    byhand.clock_moved(-1000.0)
    notarget.clock_moved(-1000.0)
    assert byhand._stretch["hlt"]["since"] == 4000.0
    assert byhand._stretch["bk"]["since"] is None
    assert notarget._since == {"k": 4000.0}


# --- the kettles ------------------------------------------------------------------------------------------------------

class Moves:
    def __init__(self):
        self.state, self.moved = True, []

    def clock_moved(self, seconds):
        self.moved.append(seconds)


class CantMove:
    def __init__(self, state=True):
        self.state = state
        self.grain_present, self.pump_hold = False, True


def kettles(*instances):
    controller = KettleController.__new__(KettleController)
    controller.cbpi = mock.Mock()
    controller.data = [Kettle(id="k%d" % i, name="K%d" % i, instance=inst) for i, inst in enumerate(instances)]
    controller.stop = mock.AsyncMock()
    controller.start = mock.AsyncMock()
    return controller


def test_a_logic_that_moves_its_own_times_runs_on_and_one_that_cant_starts_again_with_what_a_step_told_it():
    moves, cant, off = Moves(), CantMove(), CantMove(state=False)
    controller = kettles(moves, cant, off)
    restart = controller.clock_moved(-500.0)
    assert moves.moved == [-500.0]
    assert restart == ["k1"]
    assert asyncio.run(controller.restart_after_clock_reset(restart)) == ["k1"]
    controller.stop.assert_awaited_once_with("k1")
    controller.start.assert_awaited_once_with("k1", preset={"grain_present": False, "pump_hold": True})


def test_hysteresis_keeps_nothing_on_the_clock_and_runs_on():
    from cbpi.extension.hysteresis import Hysteresis
    logic = Hysteresis(mock.Mock(), "k0", Props({}))
    logic.state = True
    assert kettles(logic).clock_moved(-500.0) == []


# --- the logs ---------------------------------------------------------------------------------------------------------

def write_rows(path, start, count, step=1.0):
    with open(path, "w", encoding="utf-8") as f:
        for i in range(count):
            stamp = datetime.fromtimestamp(start + i * step).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            f.write("%s,%.1f\n" % (stamp, 150 + i))


def logs(tmp_path):
    from cbpi.controller.log_file_controller import LogController
    cbpi = mock.Mock()
    cbpi.config_folder.logsFolderPath = str(tmp_path)
    return LogController(cbpi)


def test_the_logs_written_ahead_are_archived_and_new_rows_start_a_new_file(tmp_path):
    from cbpi.extension.SensorLogTarget_CSV import BatchingRotatingFileHandler
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_ahead.log", now + DAY, 10)
    write_rows(tmp_path / "sensor_ahead.log.1", now + DAY - 100, 10)
    write_rows(tmp_path / "sensor_behind.log", now - 100, 10)
    log = logs(tmp_path)
    data_logger = logging.getLogger("cbpi.sensor.test-ahead")
    data_logger.propagate = False
    data_logger.setLevel(logging.DEBUG)
    handler = BatchingRotatingFileHandler(str(tmp_path / "sensor_ahead.log"), maxBytes=500000, backupCount=5,
                                          flush_seconds=60)
    data_logger.addHandler(handler)
    held = now + 2 * DAY  # buffered, not on disk yet
    data_logger.info("%s,170.0" % datetime.fromtimestamp(held).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
    log.datalogger["ahead"] = data_logger
    log._last_change = {"ahead": 1, "behind": 2}

    async def archive():
        archived = log.archive_logs_ahead(now)
        await log.archive_zipping
        return archived

    archived = asyncio.run(archive())

    assert archived["sensors"] == ["ahead"]
    folder = archived["folder"]
    assert sorted(os.listdir(folder)) == ["sensor_ahead.log", "sensor_ahead.log.1"]
    assert "170.0" in (open(os.path.join(folder, "sensor_ahead.log"), encoding="utf-8").read())
    assert not (tmp_path / "sensor_ahead.log").exists()
    assert (tmp_path / "sensor_behind.log").exists()
    assert "ahead" not in log.datalogger and data_logger.handlers == []
    assert log._last_change == {"behind": 2}
    assert len(archived["zips"]) == 1 and (tmp_path / archived["zips"][0]).exists()
    assert archived["latest"] == pytest.approx(held, abs=0.01)
    # A reading already on its way to the closed file opens nothing again.
    handler.handle(logging.makeLogRecord(dict(msg="2026-11-24 23:15:01.000,171.0", levelno=logging.INFO)))
    handler.flush()
    assert not (tmp_path / "sensor_ahead.log").exists()


def test_no_log_ahead_archives_nothing(tmp_path):
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_behind.log", now - 100, 10)
    assert logs(tmp_path).archive_logs_ahead(now) is None
    assert not (tmp_path / "archive").exists()


def test_a_log_held_open_for_a_moment_is_still_archived(tmp_path):
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_ahead.log", now + DAY, 10)
    log = logs(tmp_path)
    real, refused = os.replace, []

    def held_twice(source, target):
        if len(refused) < 2:
            refused.append(source)
            raise PermissionError(32, "The process cannot access the file because it is being used")
        real(source, target)

    with mock.patch("cbpi.controller.log_file_controller.os.replace", held_twice), \
            mock.patch("cbpi.controller.log_file_controller.time.sleep"):
        archived = log.archive_logs_ahead(now)
    assert len(refused) == 2 and archived["sensors"] == ["ahead"]
    assert not (tmp_path / "sensor_ahead.log").exists()
    assert os.path.exists(os.path.join(archived["folder"], "sensor_ahead.log"))


def test_rows_still_held_in_memory_count(tmp_path):
    from cbpi.extension.SensorLogTarget_CSV import BatchingRotatingFileHandler
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_held.log", now - 100, 10)
    log = logs(tmp_path)
    data_logger = logging.getLogger("cbpi.sensor.test-held")
    data_logger.propagate = False
    data_logger.setLevel(logging.DEBUG)
    data_logger.addHandler(BatchingRotatingFileHandler(str(tmp_path / "sensor_held.log"), maxBytes=500000,
                                                       backupCount=5, flush_seconds=60))
    stamp = datetime.fromtimestamp(now + DAY).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    data_logger.info("%s,170.0" % stamp)
    log.datalogger["held"] = data_logger
    archived = log.archive_logs_ahead(now)
    assert archived is not None and archived["sensors"] == ["held"]
    assert not (tmp_path / "sensor_held.log").exists()


# --- the whole reset --------------------------------------------------------------------------------------------------

def server():
    cbpi = mock.Mock()
    cbpi.step.save = mock.AsyncMock()
    cbpi.step.replan_after_clock_reset = mock.AsyncMock(return_value=False)
    cbpi.kettle.clock_moved = mock.Mock(return_value=["k1"])
    cbpi.kettle.restart_after_clock_reset = mock.AsyncMock(return_value=["k1"])
    cbpi.kettle.push_udpate = mock.AsyncMock()
    cbpi.log.archive_logs_ahead = mock.Mock(return_value=None)
    cbpi.plugin.extensions = []
    return cbpi


def test_the_reset_tells_every_screen_and_says_so(pc):
    assert clock_controller is not None, "no cbpi.controller.clock_controller"
    ahead_by_two_days(pc)
    was = clock.now()
    cbpi = server()
    result = asyncio.run(clock_controller.ClockController(cbpi).reset(1))
    sent = [c.args[0] for c in cbpi.ws.send.call_args_list if c.args[0].get("topic") == "clock"]
    assert len(sent) == 1
    data = sent[0]["data"]
    assert data["now"] == pytest.approx(pc.now())
    assert data["scale"] == 1.0
    assert data["was"] == {"now": pytest.approx(was), "scale": 30.0}
    assert data["moved"] == pytest.approx(-2 * DAY)
    assert result["restarted"] == ["k1"]
    cbpi.step.clock_moved.assert_called_once()
    cbpi.timers.clock_moved.assert_called_once()
    cbpi.step.replan_after_clock_reset.assert_awaited_once()
    cbpi.kettle.restart_after_clock_reset.assert_awaited_once_with(["k1"])
    cbpi.log.archive_logs_ahead.assert_called_once()
    title, message = cbpi.notify.call_args.args[:2]
    assert title == "Clock"
    assert message == ("CraftBeerPi's clock is back to this computer's time, {}, now running 1\u00d7 (was 30\u00d7)."
                       .format(clock_controller.clock_time(pc.now())))


def test_a_change_of_speed_at_the_computers_time_starts_nothing_again(pc):
    cbpi = server()
    asyncio.run(clock_controller.ClockController(cbpi).reset(60))
    assert clock.scale() == 60.0
    cbpi.step.clock_moved.assert_called_once()
    cbpi.kettle.restart_after_clock_reset.assert_not_awaited()
    cbpi.step.replan_after_clock_reset.assert_not_awaited()


def test_a_speed_the_clock_cannot_run_at_changes_and_says_nothing(pc):
    cbpi = server()
    assert asyncio.run(clock_controller.ClockController(cbpi).reset("fast")) is None
    cbpi.ws.send.assert_not_called()
    cbpi.notify.assert_not_called()


def test_the_notification_says_the_archive_too(pc):
    ahead_by_two_days(pc)
    cbpi = server()
    cbpi.log.archive_logs_ahead = mock.Mock(return_value=dict(
        sensors=["a"], latest=pc.now() + DAY, folder="X:\\logs\\archive\\run-simulation", zips=["z.zip"]))
    asyncio.run(clock_controller.ClockController(cbpi).reset(1))
    message = cbpi.notify.call_args.args[1]
    assert "were archived" in message and "z.zip" in message and "run-simulation" in message


def test_the_time_and_speed_as_the_brewer_reads_them():
    moment = datetime(2026, 11, 22, 23, 18, 5).timestamp()
    assert clock_controller.clock_time(moment, "24-hour") == "Sun 22 Nov 23:18"
    assert clock_controller.clock_time(moment, "12-hour") == "Sun 22 Nov 11:18 PM"
    assert clock_controller.clock_time(datetime(2026, 12, 1, 7, 5).timestamp(), "24-hour") == "Tue 1 Dec 07:05"
    cbpi = mock.Mock()
    cbpi.config.get.return_value = "12-hour"
    assert clock_controller.clock_time(moment, cbpi) == "Sun 22 Nov 11:18 PM"
    assert [clock_controller.speed(s) for s in (30.0, 1, 0.5, 60)] == ["30\u00d7", "1\u00d7", "0.5\u00d7", "60\u00d7"]


def test_the_notification_follows_clock_format(pc):
    cbpi = server()
    cbpi.config.get.side_effect = lambda key, default=None: "12-hour" if key == "CLOCK_FORMAT" else default
    asyncio.run(clock_controller.ClockController(cbpi).reset(1))
    message = cbpi.notify.call_args.args[1]
    assert message.startswith("CraftBeerPi's clock is back to this computer's time, {}, now running 1\u00d7 "
                              "(was 30\u00d7).".format(clock_controller.clock_time(pc.now(), "12-hour")))
    assert message.split(", ")[1].endswith(("AM", "PM"))
