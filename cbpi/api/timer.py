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
        self._deadline = None
        self._started = False

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
        # One mutable deadline, on the instance, because add() has to be able to
        # move it. Holding it in a local was how "Add 5 Minutes to Timer" came
        # to do nothing at all: add() updated self.end_time, which is only ever
        # read for display, and the loop kept waiting on the value it had
        # captured when it started.
        self._deadline = start + duration
        self.end_time = int(round(self._deadline, 0))
        self.count = int(round(duration, 0))
        try:
            while True:
                remaining = self._deadline - clock.now()
                self.count = max(0, int(round(remaining, 0)))
                if self._update is not None:
                    await self._update(self, self.count)
                if remaining <= 0:
                    return
                # Whichever comes first: the next time the display should tick,
                # or the end. The end always wins, so a coarse refresh rate
                # cannot make the timer run long.
                #
                # Re-read each pass, so time added mid-rest takes effect: the
                # sleep still wakes at the old deadline, and the next iteration
                # simply finds there is more to do.
                await clock.sleep_until(
                    min(clock.now() + self.UPDATE_INTERVAL, self._deadline)
                )
        except asyncio.CancelledError:
            self._timemout = max(0.0, self._deadline - clock.now())
            # Re-raised so the task is actually marked cancelled. Swallowing it
            # left the task "completed", which is what made done() treat a stop as
            # an expiry. The remaining time above is still recorded first, so
            # resuming the timer picks up where it left off.
            raise

    async def add(self, seconds):
        """Extend the rest, whether or not it is currently counting down."""
        if self.is_running:
            self._deadline += seconds
            self.end_time = int(round(self._deadline, 0))
            self.count = max(0, int(round(self._deadline - clock.now(), 0)))
        else:
            # Not started, or already stopped. Extending what it will run for
            # next time is the only meaning available.
            self._timemout = max(0.0, float(self._timemout) + seconds)
            if self.end_time is not None:
                self.end_time = self.end_time + seconds

    def start(self):
        self._started = True
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
        # Stopped is stopped, whether or not there was a live task to cancel.
        # This is what lets a stopped step be started again.
        self._started = False

    def reset(self):
        # Guarded on the task, not on is_running. is_running stays true after a
        # countdown finishes - see below - so guarding on it is what made reset
        # a permanent no-op.
        if self._task_alive():
            return
        self._started = False
        self._timemout = self.timeout

    def _task_alive(self):
        """Is the countdown task actually running right now?"""
        return self._task is not None and not self._task.done()

    @property
    def is_running(self):
        """Has this timer been started and not stopped?

        Deliberately NOT "is the countdown task alive". Every caller in the
        codebase uses this to decide whether to call start():

            if self.timer.is_running is not True:
                self.timer.start()

        and those checks sit inside run() loops that keep iterating after the
        countdown has finished. Reporting False once the task completes means
        the next iteration starts the rest again - a sixty minute mash that
        quietly runs twice. That was measured: an end-to-end brew day went from
        410 to 952 seconds before this distinction was drawn.

        It was previously a plain method that every step overwrote with a
        boolean, which worked by accident - a bound method is not the singleton
        True - and left reset() and set_time() permanently disabled, because
        nothing ever set the boolean back to False.
        """
        return self._started

    @is_running.setter
    def is_running(self, value):
        # Accepted and ignored. Every step assigns True here immediately after
        # calling start(), which start() has already recorded. Rewriting them
        # all is a wider change than it looks, and the assignments are now
        # harmless rather than destructive.
        pass

    def set_time(self, timeout):
        if self._task_alive():
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
