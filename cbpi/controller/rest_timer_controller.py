"""The brew-day screen's rest timer, kept here so every screen shows the same one.

Brewing by hand, with no recipe running, the brew-day screen times a rest:
START TIMER, PAUSE TIMER, and five minutes more or less. Scott, 9 Nov 2026,
asked for one timer shared by the monitor and the phone ("Share one timer
between screens"), so the time left and whether it is running live in the
server, and the screens only send commands and show what they are told.

At zero it raises a CraftBeerPi notification with an OK button, which every
screen shows and keeps until somebody answers it, as hop alerts do.

It runs on the brewing clock (cbpi.api.clock): on a simulator at SIM_TIME_SCALE
30 a rest goes 30 times faster, like everything else there. The screens count
down between messages with `scale`, and correct themselves on each message.

Kept in memory only: after a restart the timer is back at 60:00 and stopped.
"""

import asyncio
import logging

from cbpi.api import clock
from cbpi.api.dataclasses import NotificationAction, NotificationType


class RestTimerController:
    #: The websocket topic every screen hears: data is get_state().
    TOPIC = "resttimer"
    #: A fresh timer, as the screen first shows it: 60:00.
    START = 60 * 60
    #: Five minutes more or less never takes it below one minute or past five
    #: hours (the approved mockup's limits).
    MIN = 60
    MAX = 5 * 60 * 60
    TITLE = "Rest timer"
    MESSAGE = "The rest timer has finished."

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        self.remaining = float(self.START)
        self.running = False
        self._deadline = None  # clock.monotonic() at zero, while running
        self._task = None
        self._prompt = None  # the "finished" notification, while it waits

    def left(self) -> float:
        """Seconds to go, on the brewing clock."""
        if self.running and self._deadline is not None:
            return max(0.0, self._deadline - clock.monotonic())
        return max(0.0, self.remaining)

    def get_state(self):
        return dict(remaining=round(self.left(), 3), running=self.running, scale=clock.scale())

    def _push(self):
        try:
            self.cbpi.ws.send(dict(topic=self.TOPIC, data=self.get_state()))
        except Exception as e:  # noqa: BLE001 - a screen that misses this hears the next one
            self.logger.debug("rest timer: could not tell the screens: %s", e)

    def _retire_prompt(self):
        """A new rest makes the last one's "finished" no longer apply."""
        if self._prompt is not None:
            try:
                self.cbpi.notification.retire(self._prompt)
            except Exception as e:  # noqa: BLE001
                self.logger.debug("rest timer: could not take the old prompt down: %s", e)
            self._prompt = None

    def _cancel(self):
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    def _arm(self):
        self._cancel()
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def start(self):
        """Count down from where it stands. Raises ValueError at zero: there is
        nothing to count, so add time first."""
        if self.running:
            return self.get_state()
        if self.left() <= 0:
            raise ValueError("The rest timer is at zero. Add time first.")
        self._retire_prompt()
        self._deadline = clock.monotonic() + self.remaining
        self.running = True
        self._arm()
        self._push()
        return self.get_state()

    async def pause(self):
        if not self.running:
            return self.get_state()
        self.remaining = self.left()
        self.running = False
        self._deadline = None
        self._cancel()
        self._push()
        return self.get_state()

    async def add(self, seconds):
        """Five minutes more (300) or less (-300), kept between MIN and MAX."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            raise ValueError("seconds must be a number")
        if seconds != seconds or seconds in (float("inf"), float("-inf")):
            raise ValueError("seconds must be a number")
        left = min(float(self.MAX), max(float(self.MIN), self.left() + seconds))
        if self.running:
            # sleeping toward the old zero would end a shortened rest late
            self._deadline = clock.monotonic() + left
            self._arm()
        else:
            self.remaining = left
        self._push()
        return self.get_state()

    async def _run(self):
        try:
            scale = clock.scale()
            while self.running and self._deadline is not None and clock.monotonic() < self._deadline:
                # In slices of about a real second, as step timers wait: one long
                # sleep keeps the speed the clock had when it began, and a
                # simulator sets SIM_TIME_SCALE a moment after start-up.
                await clock.sleep_until(min(self._deadline, clock.monotonic() + max(1.0, clock.scale())))
                if clock.scale() != scale:
                    scale = clock.scale()
                    self._push()  # the screens count down at the new speed
            if self.running:
                await self._finish()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a timer that dies says so instead of sitting at zero forever
            self.logger.error("rest timer stopped: %s", e)
            self.remaining = self.left()
            self.running = False
            self._deadline = None
            self._push()

    async def _finish(self):
        self.running = False
        self.remaining = 0.0
        self._deadline = None
        self._task = None
        self._retire_prompt()
        self._prompt = self.cbpi.notify(
            self.TITLE,
            self.MESSAGE,
            NotificationType.WARNING,
            action=[NotificationAction("OK", self._answered)],
            keep=True,
        )
        self._push()

    async def _answered(self):
        self._prompt = None
