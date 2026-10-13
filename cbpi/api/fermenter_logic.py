import asyncio
import logging
import time
from abc import ABCMeta

from cbpi.api.base import CBPiBase
from cbpi.api.extension import CBPiExtension


class CBPiFermenterLogic(CBPiBase, metaclass=ABCMeta):
    """A fermenter's temperature controller, kept alive for weeks unattended.

    A fermenter runs for weeks with nobody watching, so the one failure this
    must never have is the one it had: run() ending - an exception, a bug, a
    return - while the interface went on showing the logic as running. _run()
    caught only CancelledError, so the task simply finished, `state` stayed
    True, and the fermenter drifted to ambient with a green switch on screen
    (DEFECTS F8).

    Now a run() that ends without stop() asking it to is a failure: its
    actors are switched off, the brewer is told, and it is started again after
    a delay that doubles with each consecutive failure. Only stop() - the
    switch, AutoMode, a deleted fermenter, shutdown - ends it for good.
    """

    # Seconds before a failed run() is started again, doubling per
    # consecutive failure up to the cap. Wall clock on purpose: this paces
    # retries of a broken controller and measures nothing about the beer.
    RESTART_DELAY_MIN = 5
    RESTART_DELAY_MAX = 300

    # A run() that lasted this long before failing was working, so the next
    # failure starts the backoff from the minimum again.
    RESTART_HEALTHY_AFTER = 600

    # At most one restart notification per this many seconds. A logic failing
    # every few minutes is worth hearing about hourly, not every few minutes.
    RESTART_NOTIFY_EVERY = 3600

    def __init__(self, cbpi, id, props):
        self.cbpi = cbpi
        self.id = id
        self.props = props
        self.state = False
        self.running = False
        # Set by stop(), and only by a deliberate stop: anything else that
        # ends run() is a failure to recover from.
        self.stop_requested = False
        self.task = None
        self.restarts = 0
        self.last_failure = None
        self._restart_notified_at = None

    def init(self):
        pass

    async def on_start(self):
        pass

    async def on_stop(self):
        pass

    async def run(self):
        pass

    def is_alive(self):
        """Is this logic's task still going - running, or waiting to restart?"""
        return self.task is not None and not self.task.done()

    async def _run(self):
        delay = self.RESTART_DELAY_MIN
        while not self.stop_requested:
            started = time.monotonic()
            failure = None
            self.running = True
            try:
                await self.on_start()
                self.cancel_reason = await self.run()
            except asyncio.CancelledError:
                await self._on_stop_quietly()
                return
            except Exception as e:  # noqa: BLE001 - recovered from below
                failure = e
            await self._on_stop_quietly()
            if self.stop_requested:
                return

            if time.monotonic() - started >= self.RESTART_HEALTHY_AFTER:
                delay = self.RESTART_DELAY_MIN
            self.restarts += 1
            self.last_failure = failure
            await self._report_unexpected_end(failure, delay)
            if not await self._wait_to_restart(delay):
                return
            delay = min(delay * 2, self.RESTART_DELAY_MAX)

    async def _on_stop_quietly(self):
        try:
            await self.on_stop()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - a failing hook must not stop recovery
            logging.error("Fermenter logic %s: on_stop failed: %s", self.id, e)

    async def _wait_to_restart(self, delay):
        """Sleep `delay` wall-clock seconds; False if stopped meanwhile."""
        deadline = time.monotonic() + delay
        try:
            while not self.stop_requested and time.monotonic() < deadline:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return False
        return not self.stop_requested

    def _fermenter_name(self):
        try:
            return getattr(self.get_fermenter(self.id), "name", None) or str(self.id)
        except Exception:  # noqa: BLE001
            return str(self.id)

    async def _deenergize(self):
        """Every actor this fermenter has, off - whatever the logic left them in."""
        try:
            fermenter = self.get_fermenter(self.id)
        except Exception:  # noqa: BLE001
            fermenter = None
        for role in ("heater", "cooler", "valve"):
            actor_id = getattr(fermenter, role, None)
            if actor_id:
                await self.actor_off(actor_id)

    async def _report_unexpected_end(self, failure, delay):
        name = self._fermenter_name()
        what = "{}: {}".format(type(failure).__name__, failure) if failure else "it returned"
        logging.error(
            "Fermenter %s: temperature control stopped unexpectedly (%s). "
            "Actors off, restarting in %ss (restart %d).",
            name, what, delay, self.restarts, exc_info=failure,
        )
        await self._deenergize()
        now = time.monotonic()
        if (self._restart_notified_at is None
                or now - self._restart_notified_at >= self.RESTART_NOTIFY_EVERY):
            self._restart_notified_at = now
            try:
                from cbpi.api.dataclasses import NotificationType

                self.cbpi.notify(
                    "{} temperature control".format(name),
                    "Stopped unexpectedly ({}). Heating and cooling are off; "
                    "restarting in {}s.".format(what, delay),
                    NotificationType.ERROR,
                )
            except Exception as e:  # noqa: BLE001
                logging.error("Could not notify about fermenter %s: %s", name, e)

    def get_state(self):
        return dict(running=self.state)

    async def start(self):
        self.stop_requested = False
        self.state = True

    async def stop(self):
        self.stop_requested = True
        self.running = False
        task = self.task
        if task is not None and not task.done():
            if task is asyncio.current_task():
                # Stopping itself from inside run(): the loop sees the flags
                # above and ends. Cancelling here would cancel this very call.
                pass
            else:
                task.cancel()
                # wait(), not `await task`: a task cancelled before it ever
                # ran raises CancelledError when awaited, and so did one whose
                # run() had failed - which left `state` True and the switch
                # showing a logic that had stopped.
                await asyncio.wait({task})
        self.state = False
