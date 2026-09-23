import asyncio
import math
import time

from cbpi.api import clock


class Timer(object):

    #: How often to report the countdown, in simulated seconds. This is a
    #: display cadence and nothing more - it must never decide when the timer
    #: finishes, which is what `end_time` is for. Fusing the two is what made a
    #: twelve second timer measure fifteen: the loop slept a fixed step and
    #: checked afterwards, so it always ran on past its deadline by whatever the
    #: last step overshot by, multiplied by the time scale.
    UPDATE_INTERVAL = 1.0

    def __init__(self, timeout, on_done=None, on_update=None) -> None:
        super().__init__()
        self.timeout = timeout
        self._timemout = self.timeout
        self._task = None
        self._callback = on_done
        self._update = on_update
        self.start_time = None
        self.end_time = None

    def done(self, task):
        # A cancelled timer has not finished, it was stopped. Firing the completion
        # callback either way made a deliberate stop indistinguishable from expiry,
        # and on_timer_done() is where steps act on "the rest is over" - switching
        # AutoMode off, notifying the brewer, and in NotificationStep with
        # AutoNext=Yes, calling next(). That could walk a profile forward while the
        # server was shutting down.
        if task.cancelled():
            return
        if self._callback is not None:
            asyncio.create_task(self._callback(self))

    async def _job(self):
        # Simulated time throughout. A mash rest is a duration in brewing time, not
        # in wall clock: on a real rig the two are the same thing, and on a
        # simulated one a sixty minute rest should take sixty simulated minutes
        # however fast the model is being run. Measuring this in wall clock is what
        # made a condensed brew day impossible - the physics compressed, the rests
        # did not, and a "fast" run still took its full number of hours.
        #
        # The rest is expressed as a deadline rather than as a series of one
        # second sleeps. Those are not equivalent: sleeping a fixed step and
        # checking afterwards always finishes somewhere past the end, by however
        # much the final step overshot, and a scaled clock multiplies that by the
        # scale. Waiting for an instant instead lets the clock decide how to get
        # there - which for a virtual clock is to jump exactly onto it.
        start = clock.now()
        self.start_time = int(start)
        duration = max(0.0, float(self._timemout))
        deadline = start + duration
        self.end_time = int(round(deadline, 0))
        self.count = int(round(duration, 0))
        try:
            while True:
                remaining = deadline - clock.now()
                self.count = max(0, int(round(remaining, 0)))
                if self._update is not None:
                    await self._update(self, self.count)
                if remaining <= 0:
                    return
                # Whichever comes first: the next time the display should tick,
                # or the end. The end always wins, so a coarse refresh rate
                # cannot make the timer run long.
                await clock.sleep_until(
                    min(clock.now() + self.UPDATE_INTERVAL, deadline)
                )
        except asyncio.CancelledError:
            self._timemout = max(0.0, deadline - clock.now())
            # Re-raised so the task is actually marked cancelled. Swallowing it
            # left the task "completed", which is what made done() treat a stop as
            # an expiry. The remaining time above is still recorded first, so
            # resuming the timer picks up where it left off.
            # Re-raised so the task is actually marked cancelled. Swallowing it
            # left the task "completed", which is what made done() treat a stop as
            # an expiry. The remaining time above is still recorded first, so
            # resuming the timer picks up where it left off.
            raise

    async def add(self, seconds):
        self.end_time = self.end_time + seconds

    def start(self):
        self._task = asyncio.create_task(self._job())
        self._task.add_done_callback(self.done)

    async def stop(self):
        if self._task and self._task.done() is False:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected - this is the cancellation we just asked for. Swallowed
                # here, at the point where it was requested, so callers can stop a
                # timer without handling it. _job() still re-raises, which is what
                # marks the task cancelled and lets done() tell a stop from an
                # expiry.
                pass

    def reset(self):
        if self.is_running is True:
            return
        self._timemout = self.timeout

    def is_running(self):
        return not self._task.done()

    def set_time(self, timeout):
        if self.is_running is True:
            return
        self.timeout = timeout

    def get_time(self):
        return self.format_time(int(round(self._timemout, 0)))

    @classmethod
    def format_time(cls, time):
        pattern_h = "{0:02d}:{1:02d}:{2:02d}"
        pattern_d = "{0:02d}D {1:02d}:{2:02d}:{3:02d}"
        seconds = time % 60
        minutes = math.floor(time / 60) % 60
        hours = math.floor(time / 3600) % 24
        days = math.floor(time / 86400)
        if days != 0:
            remaining_time = pattern_d.format(days, hours, minutes, seconds)
        else:
            remaining_time = pattern_h.format(hours, minutes, seconds)
        return remaining_time
