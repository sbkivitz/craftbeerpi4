"""With NOTIFY_ON_ERROR on, a WARNING or ERROR log line raises a notification (and a phone alert through the
listeners). A line logged again and again - a fault logging every control pass - is notified once an hour; a line that
has gone unlogged for 5 minutes has cleared, and is notified at once when it comes back. A listener that takes log_line
is told which: "new" the first time and each time it comes back, "still" for the hour's reminder, None for any other
notification. Timed on CraftBeerPi's clock (cbpi.api.clock), driven here by hand."""

import asyncio
import logging
import types

import pytest

from cbpi.api import clock
from cbpi.api.dataclasses import NotificationType
from cbpi.controller.notification_controller import NotificationController

HOUR = 3600.0
MINUTE = 60.0


class Manual(clock.TimeSource):
    """A clock moved by hand."""

    def __init__(self, now=1_795_400_000.0, mono=10_000.0):
        self._now, self._mono = float(now), float(mono)

    def now(self):
        return self._now

    def monotonic(self):
        return self._mono

    def wait(self, seconds):
        self._now += seconds
        self._mono += seconds

    async def sleep(self, seconds):
        await asyncio.sleep(0)


@pytest.fixture
def hand():
    source = Manual()
    previous = clock.set_source(source)
    yield source
    clock.set_source(previous)


def notifier():
    sent = []
    controller = NotificationController.__new__(NotificationController)
    controller.cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: "Yes" if key == "NOTIFY_ON_ERROR" else default),
        notify=lambda **k: sent.append((k["title"], k["message"])))
    return controller, sent


def log(controller, message="Actor pump reports still ON after being switched off", level=logging.ERROR):
    controller.notify_log_event(logging.LogRecord("root", level, __file__, 1, message, None, None))


def every(controller, source, seconds, step):
    """The stuck pump's line logged every `step` seconds for `seconds`, starting now."""
    for _ in range(int(seconds / step)):
        log(controller)
        source.wait(step)


def test_a_line_logged_every_second_is_notified_once_an_hour(hand):
    controller, sent = notifier()
    every(controller, hand, HOUR, 1.0)
    assert len(sent) == 1
    log(controller)
    assert len(sent) == 2
    hand.wait(1.0)
    every(controller, hand, HOUR - 1.0, 1.0)
    assert len(sent) == 2
    log(controller)
    assert len(sent) == 3


def test_a_line_back_after_six_quiet_minutes_is_notified_at_once(hand):
    controller, sent = notifier()
    every(controller, hand, 10 * MINUTE, 1.0)
    assert len(sent) == 1
    hand.wait(6 * MINUTE)
    log(controller)
    assert len(sent) == 2
    hand.wait(1.0)
    every(controller, hand, 10 * MINUTE, 1.0)
    assert len(sent) == 2


def test_a_line_back_after_two_quiet_minutes_stays_quiet_until_its_hour(hand):
    controller, sent = notifier()
    log(controller)
    hand.wait(2 * MINUTE)
    log(controller)
    hand.wait(299.0)
    log(controller)
    assert len(sent) == 1
    hand.wait(300.0)
    log(controller)
    assert len(sent) == 2
    # Coming and going every 2 minutes, it never clears: once an hour.
    controller, sent = notifier()
    every(controller, hand, 2 * HOUR + 1.0, 2 * MINUTE)
    assert len(sent) == 2
    log(controller)
    assert len(sent) == 3


def test_different_lines_are_notified_each_on_its_own(hand):
    controller, sent = notifier()
    for second in range(int(10 * MINUTE)):
        log(controller)
        if second in (0, 400):
            log(controller, "Sensor HLT has not answered for 60 s", logging.WARNING)
        if second in (0, 100):
            log(controller, "Actor pump reports still ON after being switched off", logging.WARNING)
        hand.wait(1.0)
    assert sent == [("ERROR", "Actor pump reports still ON after being switched off"),
                    ("WARNING", "Sensor HLT has not answered for 60 s"),
                    ("WARNING", "Actor pump reports still ON after being switched off"),
                    ("WARNING", "Sensor HLT has not answered for 60 s")]


def test_the_lines_remembered_are_bounded_and_the_one_logged_longest_ago_goes_first(hand):
    controller, sent = notifier()
    for n in range(controller.LOG_NOTIFY_REMEMBERED + 1):
        log(controller, f"Sensor {n} has not answered")
    assert len(controller._log_lines) == controller.LOG_NOTIFY_REMEMBERED
    log(controller, "Sensor 0 has not answered")
    assert sent[-1][1] == "Sensor 0 has not answered" and len(sent) == controller.LOG_NOTIFY_REMEMBERED + 2
    # A fault logged every second stays remembered while more lines than are kept come and go around it.
    controller, sent = notifier()
    for n in range(3 * controller.LOG_NOTIFY_REMEMBERED):
        log(controller)
        log(controller, f"Sensor {n} has not answered")
        hand.wait(1.0)
    assert len(controller._log_lines) == controller.LOG_NOTIFY_REMEMBERED
    assert [m for _, m in sent].count("Actor pump reports still ON after being switched off") == 1


def test_the_hour_and_the_quiet_minutes_are_on_craftbeerpis_clock():
    computer = Manual()
    previous = clock.set_source(clock.ScaledTimeSource(60, reference=computer))
    try:
        controller, sent = notifier()
        for second in range(61):
            log(controller)
            if second < 60:
                computer.wait(1.0)
        assert len(sent) == 2  # a real minute at 60x is an hour
        computer.wait(6.0)
        log(controller)
        assert len(sent) == 3  # six real seconds at 60x are six quiet minutes
        # A simulator's speed change sets the date back to the computer's: monotonic() goes on, the fault stays
        # one notification an hour, and is neither silenced nor repeated by it.
        before = clock.monotonic()
        assert clock.reset(1) < 0 and clock.monotonic() == pytest.approx(before)
        for _ in range(int(HOUR) - 1):
            computer.wait(1.0)
            log(controller)
        assert len(sent) == 3
        computer.wait(1.0)
        log(controller)
        assert len(sent) == 4
    finally:
        clock.set_source(previous)


def test_a_clock_put_behind_counts_as_the_line_coming_back_once(hand):
    controller, sent = notifier()
    every(controller, hand, 10 * MINUTE, 1.0)
    assert len(sent) == 1
    clock.set_source(Manual(mono=hand.monotonic() - 2 * HOUR))
    behind = clock.get_source()
    every(controller, behind, 10 * MINUTE, 1.0)
    assert len(sent) == 2
    behind.wait(2 * MINUTE)
    log(controller)
    assert len(sent) == 2
    clock.set_source(Manual(mono=hand.monotonic() + 2 * HOUR))
    every(controller, clock.get_source(), 10 * MINUTE, 1.0)
    assert len(sent) == 3


def sending():
    """A controller whose notify() is the real one, sending to nowhere, with two listeners: one that takes log_line,
    told what each notification was, and one written before log_line, told the same five things as ever."""
    told, plain = [], []
    controller = NotificationController.__new__(NotificationController)
    controller.cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: "Yes" if key == "NOTIFY_ON_ERROR" else default),
        ws=types.SimpleNamespace(send=lambda *a, **k: None), push_update=lambda *a, **k: None)
    controller.cbpi.notify = lambda title, message, type, action=[], keep=False: controller.notify(
        title, message, type, action, keep=keep)
    controller.callback_cache, controller.listener, controller.notifications = {}, {}, []
    controller.update_key, controller.sorting = "notificationupdate", False

    async def listener(cbpi, title, message, type, action, log_line=None):
        told.append((message, log_line))

    async def older(cbpi, title, message, type, action):
        plain.append(message)

    controller.add_listener(listener)
    controller.add_listener(older)
    return controller, told, plain


async def settled():
    for _ in range(5):
        await asyncio.sleep(0)


def test_listeners_are_told_whether_a_log_line_is_new_back_or_still_being_logged(hand):
    stuck = "Actor pump reports still ON after being switched off"

    async def run():
        controller, told, plain = sending()
        log(controller)
        await settled()
        assert told == [(stuck, "new")]
        every(controller, hand, 10 * MINUTE, 1.0)
        await settled()
        assert len(told) == 1
        hand.wait(6 * MINUTE)  # cleared, and back
        log(controller)
        await settled()
        assert told[1:] == [(stuck, "new")]
        hand.wait(1.0)
        every(controller, hand, HOUR - 1.0, 1.0)
        log(controller)  # an hour on, never cleared
        await settled()
        assert told[2:] == [(stuck, "still")]
        controller.notify("Boil", "Could not switch Boil Kettle off.", NotificationType.WARNING)
        await settled()
        assert told[3:] == [("Could not switch Boil Kettle off.", None)]
        assert plain == [stuck, stuck, stuck, "Could not switch Boil Kettle off."]

    asyncio.run(run())


def test_a_notification_a_listener_sends_of_its_own_is_not_a_log_lines(hand):
    async def run():
        controller, told, _ = sending()

        async def answers(cbpi, title, message, type, action, log_line=None):
            if log_line is not None:
                cbpi.notify("Phone alerts", "Sent to the phone.", NotificationType.INFO)

        controller.add_listener(answers)
        log(controller, "Sensor HLT has not answered for 60 s", logging.WARNING)
        await settled()
        assert sorted(told, key=str) == [("Sensor HLT has not answered for 60 s", "new"), ("Sent to the phone.", None)]

    asyncio.run(run())

