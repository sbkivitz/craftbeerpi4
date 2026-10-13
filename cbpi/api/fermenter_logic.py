import asyncio
import logging
import time
from abc import ABCMeta

from cbpi.api.base import CBPiBase
from cbpi.api.extension import CBPiExtension


async def bounded(awaitable, seconds):
    """Await `awaitable`, giving up after `seconds` with TimeoutError.

    asyncio.timeout where there is one (3.11+): it runs the awaitable in the
    current task. 3.11's wait_for runs it in a new task instead, so code
    inside it - an actor driver - no longer looked like it was running in the
    logic's own task, which is what stop() checks to tell a logic stopping
    itself from one being stopped (DEFECTS F23).
    """
    timeout = getattr(asyncio, "timeout", None)
    if timeout is None:
        return await asyncio.wait_for(awaitable, seconds)
    async with timeout(seconds):
        return await awaitable


class CBPiFermenterLogic(CBPiBase, metaclass=ABCMeta):
    """A fermenter's temperature controller, kept alive for weeks unattended.

    A fermenter runs for weeks with nobody watching, so the one failure this
    must never have is the one it had: run() ending - an exception, a bug, a
    return - while the interface went on showing the logic as running. _run()
    caught only CancelledError, so the task simply finished, `state` stayed
    True, and the fermenter drifted to ambient with a green switch on screen
    (DEFECTS F8).

    Now a run() that ends without stop() asking it to is a failure: the
    brewer is told, its actors are switched off, and it is started again
    after a delay that doubles with each consecutive failure. Only stop() -
    the switch, AutoMode, a deleted fermenter, shutdown - ends it for good,
    and stop() switches off every actor the logic may have energized, on its
    way out, whatever state the task was in.
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

    # Longest one actor command may take before it is abandoned. A driver
    # that hangs must not hang the logic, its recovery or its shutdown.
    ACTOR_TIMEOUT = 10

    # Longest on_start / on_stop may take.
    HOOK_TIMEOUT = 30

    ROLES = ("heater", "cooler", "valve")

    # Defaults for anything __init__ sets, so a subclass that defines its own
    # __init__ without calling this one still runs - as one did before these
    # existed. The two sets come from __getattr__ below: a mutable class
    # attribute would be one set shared by every fermenter.
    state = False
    running = False
    stop_requested = False
    task = None
    restarts = 0
    last_failure = None
    _restart_notified_at = None
    _stopping = None

    def __getattr__(self, name):
        # Reached only when normal lookup fails.
        if name in ("owned_actors", "_own_tasks"):
            value = set()
            setattr(self, name, value)
            return value
        raise AttributeError(name)

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
        # The stop in progress. Concurrent stop() calls share it rather than
        # cancelling again: a second cancel interrupted the first one's actor
        # cleanup and left a pump on (DEFECTS F18).
        self._stopping = None
        # Tasks this logic runs besides self.task (spunding's two loops), so a
        # stop() from inside one of them is recognised as the logic stopping
        # itself.
        self._own_tasks = set()
        # Actors this logic is responsible for switching off: everything it
        # has been assigned, until it has seen them off after a reassignment.
        # Cleanup that read only the CURRENT assignment left an actor that had
        # just been reassigned away running (DEFECTS F19).
        self.owned_actors = set()

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

    def _cancelled_by_request(self):
        """Is the CancelledError being handled a cancellation of THIS task?

        An awaited operation can raise CancelledError without anyone having
        cancelled the logic - a driver awaiting something that was cancelled.
        Treating that as a stop ended control for good with the switch still
        on and nobody told (DEFECTS F20). Python 3.11+ counts the cancel
        requests made of a task; before that there is no way to tell, and the
        cancellation is honoured.
        """
        if self.stop_requested:
            return True
        cancelling = getattr(asyncio.current_task(), "cancelling", None)
        return True if cancelling is None else cancelling() > 0

    async def _run(self):
        delay = self.RESTART_DELAY_MIN
        # Whether this run's on_stop is still owed. A stop arrives as a
        # cancellation in the middle of run(), and on_stop has always been
        # called on a stop - a plugin's own cleanup may live there.
        on_stop_owed = False
        try:
            while not self.stop_requested:
                started = time.monotonic()
                failure = None
                self.running = True
                on_stop_owed = True
                try:
                    await bounded(self.on_start(), self.HOOK_TIMEOUT)
                    self.cancel_reason = await self.run()
                except asyncio.CancelledError as e:
                    if self._cancelled_by_request():
                        raise
                    failure = e
                except Exception as e:  # noqa: BLE001 - recovered from below
                    failure = e
                on_stop_owed = False
                await self._hook_quietly(self.on_stop)
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
        except asyncio.CancelledError:
            if on_stop_owed:
                await self._hook_quietly(self.on_stop)
            if not self.stop_requested:
                # Cancelled without stop(): the process ending, or something
                # that went around the controller. Nothing else will switch
                # this logic's actors off.
                logging.warning("Fermenter %s: temperature control was cancelled",
                                self._fermenter_name())
                await self._deenergize()
            raise
        finally:
            # _run ends only when the logic does - restarts happen inside it -
            # so the switch must say so. It went on showing on (DEFECTS F8, F23).
            self.running = False
            self.state = False

    async def _hook_quietly(self, hook):
        try:
            await bounded(hook(), self.HOOK_TIMEOUT)
        except asyncio.CancelledError:
            if self._cancelled_by_request():
                raise
            logging.error("Fermenter logic %s: %s was cancelled", self.id, hook.__name__)
        except Exception as e:  # noqa: BLE001 - a failing hook must not stop recovery
            logging.error("Fermenter logic %s: %s failed: %r", self.id, hook.__name__, e)

    async def _wait_to_restart(self, delay):
        """Sleep `delay` wall-clock seconds; False if stopped meanwhile."""
        deadline = time.monotonic() + delay
        while not self.stop_requested and time.monotonic() < deadline:
            await asyncio.sleep(1)
        return not self.stop_requested

    def _fermenter_name(self):
        try:
            return getattr(self.get_fermenter(self.id), "name", None) or str(self.id)
        except Exception:  # noqa: BLE001
            return str(self.id)

    def _assigned_actors(self, fermenter):
        """This logic's actors as the fermenter is configured right now."""
        found = (getattr(fermenter, role, None) for role in self.ROLES)
        return {actor_id for actor_id in found if actor_id}

    def _actor_on(self, actor_id):
        try:
            return self.cbpi.actor.find_by_id(actor_id).instance.state is True
        except Exception:  # noqa: BLE001 - a missing actor is not on
            return False

    async def _off_bounded(self, actor_id):
        """Switch one actor off; True once it reports off (or no longer exists)."""
        try:
            await bounded(self.actor_off(actor_id), self.ACTOR_TIMEOUT)
        except asyncio.TimeoutError:
            logging.error("Fermenter %s: switching %s off did not finish within %ss",
                          self._fermenter_name(), actor_id, self.ACTOR_TIMEOUT)
        except asyncio.CancelledError:
            if self._cancelled_by_request():
                raise
            logging.error("Fermenter %s: switching %s off was cancelled",
                          self._fermenter_name(), actor_id)
        except Exception as e:  # noqa: BLE001
            logging.error("Fermenter %s: switching %s off failed: %r",
                          self._fermenter_name(), actor_id, e)
        return not self._actor_on(actor_id)

    async def _deenergize(self):
        """Every actor this logic is or was responsible for, off. Returns those still on.

        Each independently and each bounded: one hung or failing actor must
        not keep the others on (DEFECTS F21).
        """
        try:
            fermenter = self.get_fermenter(self.id)
        except Exception:  # noqa: BLE001
            fermenter = None
        actors = self._assigned_actors(fermenter) | set(self.owned_actors)
        still_on = []
        for actor_id in list(actors):
            if await self._off_bounded(actor_id):
                if fermenter is None or actor_id not in self._assigned_actors(fermenter):
                    self.owned_actors.discard(actor_id)
            else:
                still_on.append(actor_id)
        return still_on

    def _notify(self, title, message, kind="INFO"):
        """cbpi.notify that never raises; `kind` a NotificationType or its name."""
        try:
            from cbpi.api.dataclasses import NotificationType

            if isinstance(kind, str):
                kind = getattr(NotificationType, kind)
            self.cbpi.notify(title, message, kind)
        except Exception as e:  # noqa: BLE001 - telling must never break controlling
            logging.error("Could not notify %r: %s", title, e)

    async def _report_unexpected_end(self, failure, delay):
        name = self._fermenter_name()
        what = "{}: {}".format(type(failure).__name__, failure) if failure else "it returned"
        logging.error(
            "Fermenter %s: temperature control stopped unexpectedly (%s). "
            "Actors off, restarting in %ss (restart %d).",
            name, what, delay, self.restarts, exc_info=failure,
        )
        # Told first: the cleanup below talks to hardware, and an alert that
        # waits behind a driver that never answers is never sent (DEFECTS F21).
        now = time.monotonic()
        if (self._restart_notified_at is None
                or now - self._restart_notified_at >= self.RESTART_NOTIFY_EVERY):
            self._restart_notified_at = now
            self._notify("{} temperature control".format(name),
                         "Stopped unexpectedly ({}). Heating and cooling are being "
                         "switched off; restarting in {}s.".format(what, delay), "ERROR")
        still_on = await self._deenergize()
        if still_on:
            self._notify("{} temperature control".format(name),
                         "{} did not switch off.".format(", ".join(still_on)), "ERROR")

    def get_state(self):
        return dict(running=self.state)

    async def start(self):
        self.stop_requested = False
        self._stopping = None
        self.state = True

    def _in_own_task(self):
        current = asyncio.current_task()
        return current is not None and (current is self.task or current in self._own_tasks)

    async def stop(self):
        self.stop_requested = True
        self.running = False
        try:
            if self._in_own_task():
                # Stopping itself from inside run(): the loop sees the flags
                # above and ends, cleaning up on its way out. Waiting for
                # our own task here would wait forever.
                return
            if self._stopping is None:
                self._stopping = asyncio.ensure_future(self._stop_and_clean())
            # Shielded: a caller that is itself cancelled must not take the
            # actor cleanup down with it.
            await asyncio.shield(self._stopping)
        finally:
            self.state = False

    async def _stop_and_clean(self):
        task = self.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        # Always, after the task: a task cancelled before its first turn never
        # ran any cleanup at all (DEFECTS F18), and one whose cleanup was cut
        # short left an actor on.
        still_on = await self._deenergize()
        if still_on:
            self._notify("{} temperature control".format(self._fermenter_name()),
                         "Temperature control is off, but {} did not switch off.".format(
                             ", ".join(still_on)), "ERROR")
