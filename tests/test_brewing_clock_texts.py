"""Times of day CraftBeerPi writes for people are on its own clock (cbpi.api.clock), the one every screen shows: the
notification list's stamps, the restore-error lines and a step's "Estimated completion". On a simulator that clock runs
SIM_TIME_SCALE times as fast and ahead of the computer's; these are checked with it two days ahead, at 30x."""

import asyncio
from datetime import datetime
from unittest import mock

import pytest

from cbpi.api import clock, timefmt
from cbpi.api.dataclasses import NotificationType
from cbpi.api.timer import Timer
from cbpi.controller.notification_controller import NotificationController
from cbpi.extension import mashstep

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


@pytest.fixture
def ahead():
    """CraftBeerPi's clock at 30x, two days ahead of the computer's; the clock as it was, after."""
    computer = Manual()
    previous = clock.set_source(clock.ScaledTimeSource(30, reference=computer))
    computer._now += 2 * DAY / 29
    computer._mono += 2 * DAY / 29
    assert clock.now() - computer.now() == pytest.approx(2 * DAY)
    yield computer
    clock.set_source(previous)


def notifications(cbpi):
    controller = NotificationController.__new__(NotificationController)
    controller.cbpi = cbpi
    controller.callback_cache, controller.open_prompts, controller.listener = {}, {}, {}
    controller.notifications, controller.update_key, controller.sorting = [], "notificationupdate", False
    return controller


def server(clock_format="24-hour"):
    cbpi = mock.Mock()
    cbpi.config.get.side_effect = lambda key, default=None: clock_format if key == "CLOCK_FORMAT" else default
    return cbpi


@pytest.mark.parametrize("clock_format", ["24-hour", "12-hour"])
def test_the_notification_list_is_stamped_on_craftbeerpis_clock(ahead, clock_format):
    cbpi = server(clock_format)
    controller = notifications(cbpi)

    async def send():
        controller.notify("Boil", "Add the hops", NotificationType.INFO)

    asyncio.run(send())
    stamp = timefmt.day_and_time(clock.now(), cbpi, day="%Y-%m-%d", seconds=True)
    assert controller.notifications[0] == ["{}: Boil | Add the hops".format(stamp)]


def test_the_restore_error_lines_are_stamped_on_craftbeerpis_clock(ahead, tmp_path):
    (tmp_path / "restore_error.log").write_text("settings.json could not be read\n", encoding="utf-8")
    cbpi = server()
    cbpi.config_folder.get_file_path.side_effect = lambda name: str(tmp_path / name)
    controller = notifications(cbpi)
    controller.check_startup_message()
    stamp = datetime.fromtimestamp(clock.now()).strftime("%Y-%m-%d %H:%M:%S")
    assert controller.notifications == [["{}: Restore Error | settings.json could not be read\n".format(stamp)]]


@pytest.mark.parametrize("clock_format", ["24-hour", "12-hour"])
def test_the_estimated_completion_is_a_time_on_craftbeerpis_clock(ahead, clock_format):
    timer = Timer(20 * 60)
    assert mashstep._completion_time(timer, clock_format) == timefmt.time_of_day(clock.now() + 20 * 60, clock_format)
