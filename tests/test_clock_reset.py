"""A simulator's change of speed sets CraftBeerPi's clock back to the computer's time (cbpi.api.clock.reset,
CraftBeerPi.brewing_clock), and whatever keeps a time on the clock moves with it, so the brew carries on with as long
to go: a step's timer, the brew steps' kept times, the brew-day timers, the alarms' watches; BrewStart plans again;
a kettle logic that can't move its own times starts again; each sensor log gets a row saying the clock moved, after
every row it had, so the graphs keep the readings so far; every screen is told, and a notification says it."""

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


def open_log(log, tmp_path, name):
    """`name`'s sensor log open as SensorLogTarget_CSV opens it: a batching handler on sensor_<name>.log, holding
    readings in memory for up to a minute."""
    from cbpi.extension.SensorLogTarget_CSV import BatchingRotatingFileHandler
    data_logger = logging.getLogger("cbpi.sensor.test-" + name)
    data_logger.propagate = False
    data_logger.setLevel(logging.DEBUG)
    handler = BatchingRotatingFileHandler(str(tmp_path / ("sensor_%s.log" % name)), maxBytes=500000, backupCount=5,
                                          flush_seconds=60)
    handler.clock_log = log
    data_logger.addHandler(handler)
    log.datalogger[name] = data_logger
    return data_logger, handler


def close_log(data_logger, handler):
    data_logger.removeHandler(handler)
    handler.close()


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def moved_by(line):
    from cbpi.api import logclock
    return logclock.moved_by(line.split(",", 1)[1])


def test_each_log_gets_a_row_saying_the_clock_moved_and_keeps_every_row(tmp_path):
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_ahead.log", now + DAY, 10)
    write_rows(tmp_path / "sensor_ahead.log.1", now + DAY - 100, 10)
    newest, older = lines(tmp_path / "sensor_ahead.log"), lines(tmp_path / "sensor_ahead.log.1")
    log = logs(tmp_path)
    data_logger, handler = open_log(log, tmp_path, "ahead")
    try:
        assert log.clock_moved(-2 * DAY) == ["ahead"]
        # Nothing archived, moved or deleted: every row as written, then the move's own row.
        assert sorted(os.listdir(tmp_path)) == ["sensor_ahead.log", "sensor_ahead.log.1"]
        assert lines(tmp_path / "sensor_ahead.log.1") == older
        after = lines(tmp_path / "sensor_ahead.log")
        assert after[:-1] == newest
        assert moved_by(after[-1]) == pytest.approx(-2 * DAY)
        # The log stays open, and a reading taken on the moved clock goes after the move's row.
        assert log.datalogger["ahead"] is data_logger and data_logger.handlers == [handler]
        data_logger.info("%s,171.0" % log.stamp(now - DAY + 30))
        handler.flush()
        assert lines(tmp_path / "sensor_ahead.log")[-1].endswith(",171.0")
        # Read back, the readings so far are two days earlier, on the clock as it now is, and the new one carries on.
        read = log._read_sensor_log("ahead", "v")
        expected = [now - DAY - 100 + i for i in range(10)] + [now - DAY + i for i in range(10)] + [now - DAY + 30]
        assert [t.to_pydatetime() for t in read.index] == [datetime.fromtimestamp(s) for s in expected]
        assert read["v"].iloc[-1] == 171.0
    finally:
        close_log(data_logger, handler)


def test_a_move_under_a_second_marks_no_log(tmp_path):
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_behind.log", now - 100, 10)
    before = lines(tmp_path / "sensor_behind.log")
    log = logs(tmp_path)
    assert log.clock_moved(0.004) == []
    assert log.clock_moves == []
    assert lines(tmp_path / "sensor_behind.log") == before
    assert os.listdir(tmp_path) == ["sensor_behind.log"]


def test_a_log_not_open_gets_the_row_at_the_end_of_its_newest_file(tmp_path):
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_target.log", now - 200, 10)
    write_rows(tmp_path / "sensor_target.log.1", now - 400, 10)
    newest, older = lines(tmp_path / "sensor_target.log"), lines(tmp_path / "sensor_target.log.1")
    log = logs(tmp_path)
    assert log.clock_moved(-3600.0) == ["target"]
    after = lines(tmp_path / "sensor_target.log")
    assert after[:-1] == newest
    assert moved_by(after[-1]) == pytest.approx(-3600.0)
    assert lines(tmp_path / "sensor_target.log.1") == older


def test_rows_still_held_in_memory_are_written_before_the_move_and_move_with_it(tmp_path):
    now = 1_795_400_000.0
    write_rows(tmp_path / "sensor_held.log", now - 100, 10)
    log = logs(tmp_path)
    data_logger, handler = open_log(log, tmp_path, "held")
    try:
        data_logger.info("%s,170.0" % log.stamp(now))
        assert lines(tmp_path / "sensor_held.log")[-1].endswith(",159.0"), "the reading is not held in memory"
        assert log.clock_moved(-DAY) == ["held"]
        after = lines(tmp_path / "sensor_held.log")
        assert after[-2].endswith(",170.0")
        assert moved_by(after[-1]) == pytest.approx(-DAY)
        read = log._read_sensor_log("held", "v")
        assert read.index[-1].to_pydatetime() == datetime.fromtimestamp(now - DAY)
        assert read["v"].iloc[-1] == 170.0
    finally:
        close_log(data_logger, handler)

# --- the whole reset --------------------------------------------------------------------------------------------------

def server():
    cbpi = mock.Mock()
    cbpi.step.save = mock.AsyncMock()
    cbpi.step.replan_after_clock_reset = mock.AsyncMock(return_value=False)
    cbpi.kettle.clock_moved = mock.Mock(return_value=["k1"])
    cbpi.kettle.restart_after_clock_reset = mock.AsyncMock(return_value=["k1"])
    cbpi.kettle.push_udpate = mock.AsyncMock()
    cbpi.log.clock_moved = mock.Mock(return_value=["a"])
    cbpi.plugin.extensions = []
    return cbpi


#: What the notification adds after a real move: the graphs keep what the sensor logs hold.
GRAPHS_KEPT = " The graphs keep the readings so far, moved with the clock, and carry on from them."


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
    cbpi.log.clock_moved.assert_called_once_with(pytest.approx(-2 * DAY))
    title, message = cbpi.notify.call_args.args[:2]
    assert title == "Clock"
    assert message == ("CraftBeerPi's clock is back to this computer's time, {}, now running 1\u00d7 (was 30\u00d7)."
                       .format(clock_controller.clock_time(pc.now())) + GRAPHS_KEPT)


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


def test_the_notification_says_the_graphs_keep_the_readings_only_after_a_real_move(pc):
    ahead_by_two_days(pc)
    cbpi = server()
    asyncio.run(clock_controller.ClockController(cbpi).reset(1))
    assert cbpi.notify.call_args.args[1].endswith(GRAPHS_KEPT)
    quiet = server()
    asyncio.run(clock_controller.ClockController(quiet).reset(60))
    message = quiet.notify.call_args.args[1]
    assert message.endswith("now running 60\u00d7 (was 1\u00d7).") and "graphs" not in message


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
