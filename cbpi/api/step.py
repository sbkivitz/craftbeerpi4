import asyncio
import logging
import time
from abc import abstractmethod

import cbpi
from cbpi.api.base import CBPiBase

__all__ = ["StepResult", "StepState", "StepMove", "CBPiStep", "CBPiFermentationStep"]

from enum import Enum

logging.basicConfig(
    format="%(asctime)s,%(msecs)d %(levelname)-8s [%(filename)s:%(lineno)d] %(message)s",
    datefmt="%Y-%m-%d:%H:%M:%S",
    level=logging.INFO,
)


class StepResult(Enum):
    STOP = 1
    NEXT = 2
    DONE = 3
    ERROR = 4


class StepState(Enum):
    INITIAL = "I"
    DONE = "D"
    ACTIVE = "A"
    ERROR = "E"
    STOP = "S"


class StepMove(Enum):
    UP = -1
    DOWN = 1


class CBPiStep(CBPiBase):

    # Props key holding how many seconds of this step have already run. Written
    # periodically so a restart can pick up where the step left off rather than
    # beginning a sixty minute rest again from zero. Underscored because it is
    # runtime bookkeeping, not something a user configures.
    ELAPSED_PROP = "_elapsed_seconds"

    # How often that value is written to disk. Every second would mean a JSON
    # write per second for the length of a brew; thirty seconds bounds what a
    # power cut can lose to half a minute, which is immaterial against a rest
    # measured in tens of minutes.
    ELAPSED_SAVE_INTERVAL = 30

    # A heat-up that makes no measurable progress for this long is not heating.
    # Ten minutes is comfortably longer than any real element needs to move a
    # vessel a fraction of a degree, so a working rig never trips it, while a
    # dead element or a probe lying on the bench is caught within one window.
    HEAT_STALL_WINDOW = 600

    # What counts as progress. This is deliberately tiny and unit-agnostic: the
    # question is whether the temperature is moving at all, not how fast. A
    # working element clears this in seconds.
    HEAT_STALL_MIN_RISE = 0.5

    # How close to target counts as arrived. Inside this band the watch stays
    # quiet, because a vessel being held on setpoint by a working controller is
    # not rising and must not be reported as stalled.
    #
    # This matters more than it looks. A step waits for `sensor_value >= Temp`
    # before starting its rest, so a mash settling a hundredth of a degree below
    # target sits in the waiting state indefinitely - with a controller holding
    # it there perfectly. Without this band the watch fires on the best-behaved
    # rig in the fleet, and an alert that cries wolf is worse than no alert.
    HEAT_STALL_NEAR_TARGET = 1.0

    def __init__(self, cbpi, id, name, props, on_done) -> None:
        self.name = name
        self.cbpi = cbpi
        self.id = id
        self.timer = None
        self._done_callback = on_done
        self.props = props
        self.cancel_reason: StepResult = None
        self.summary = ""
        self.summary2 = None
        self.task = None
        self.running: bool = False
        self.logger = logging.getLogger(__name__)
        self._elapsed_saved_at = 0.0
        self._stall_anchor_value = None
        self._stall_anchor_at = 0.0
        self._stall_reported = False

    def _elapsed_seconds(self):
        """Seconds of this step already run, as recorded by a previous run."""
        try:
            return max(0.0, float(self.props.get(self.ELAPSED_PROP, 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def remaining_for(self, total_seconds):
        """Timer duration for this step, honouring any elapsed time on record.

        Without this a step interrupted near its end restarts from full duration -
        a mash rest broken at minute fifty-five would run another sixty. Never
        returns less than one second, so a step that was almost over still gets a
        tick in which to finish properly rather than being skipped.
        """
        total = max(0.0, float(total_seconds))
        elapsed = self._elapsed_seconds()
        if elapsed <= 0:
            return total
        remaining = total - elapsed
        if remaining < 1:
            remaining = 1
        self.logger.info(
            "Step %s resuming with %.0fs of %.0fs remaining", self.name, remaining, total
        )
        return remaining

    async def note_progress(self, remaining_seconds, total_seconds):
        """Record progress so an interrupted step can resume where it stopped.

        Called from the per-second timer update each step already has. Throttled,
        because the point is to survive a power cut, not to write the file
        continuously.
        """
        try:
            elapsed = max(0.0, float(total_seconds) - float(remaining_seconds))
        except (TypeError, ValueError):
            return
        now = time.time()
        if now - self._elapsed_saved_at < self.ELAPSED_SAVE_INTERVAL:
            return
        self._elapsed_saved_at = now
        self.props[self.ELAPSED_PROP] = round(elapsed)
        try:
            # save() is a coroutine; calling it without awaiting silently does
            # nothing, which would have made this whole feature a no-op.
            await self.cbpi.step.save()
        except Exception as e:
            self.logger.warning("Could not record step progress: %s", e)

    def clear_progress(self):
        """Forget recorded progress, so the step starts fresh next time."""
        self._elapsed_saved_at = 0.0
        self.reset_heat_watch()
        if self.ELAPSED_PROP in self.props:
            try:
                del self.props[self.ELAPSED_PROP]
            except Exception:
                self.props[self.ELAPSED_PROP] = 0

    def reset_heat_watch(self):
        """Forget what the stall watch has seen so far."""
        self._stall_anchor_value = None
        self._stall_anchor_at = 0.0
        self._stall_reported = False

    def _heat_is_available(self):
        """Can the vessel's heat source actually deliver right now?

        Only the kettle logic knows. A HERMS mash tun has no element: it is
        heated through a coil in the HLT, so when the HLT is at or below the mash
        no heat can flow however hard the element is driven. A directly heated
        vessel has no such state, and its logic simply does not answer - which is
        read as "yes", leaving those rigs exactly as they were.

        Deliberately forgiving. Anything unexpected here means the watch stays
        quiet, because a missed alarm costs a brew and a false one costs the
        brewer's trust in every alarm afterwards.
        """
        kettle = getattr(self, "kettle", None)
        if kettle is None:
            return True
        instance = getattr(kettle, "instance", None)
        if instance is None:
            return True
        available = getattr(instance, "heat_available", None)
        if available is None:
            return True
        try:
            return bool(available)
        except Exception:  # noqa: BLE001
            return True

    def note_heat_progress(self, sensor_value, target=None):
        """Warn if a heat-up has stopped making progress.

        Steps that wait for a target temperature loop with no upper bound, so a
        failed element, a dead SSR or a probe that has fallen out of its
        thermowell leaves the rig commanding heat indefinitely with nobody told.

        The window restarts whenever the temperature actually rises, so this
        measures time since the last progress rather than time in the step - a
        legitimately slow heat-up on a cold day keeps re-anchoring and never
        trips. Reports once per stall, not once per second.

        Returns True if a stall was reported on this call.
        """
        try:
            value = float(sensor_value)
        except (TypeError, ValueError):
            # No usable reading. Sensor freshness is a separate concern; leave
            # the anchor alone so a brief dropout does not look like progress.
            return False

        now = time.time()

        # Nothing can warm from something colder than itself.
        #
        # On a HERMS the mash tun has no element: it is heated through a coil in
        # the HLT, and at every step change the target jumps while the HLT is
        # still where the last rest left it. The mash then drifts DOWN for a
        # while, flattens, and only starts to climb once the HLT has rebuilt the
        # gradient. That turning point is exactly where this watch used to fire -
        # at the moment the system is working hardest and is about to succeed.
        #
        # Observed on a running rig: the alarm was raised 21.5 F short of a
        # 168.8 F target, and the step reached target about half an hour of
        # brewing time later, having climbed all the way. Nothing was wrong.
        #
        # Only the kettle logic knows whether it can deliver heat right now, so
        # it is asked. A logic that does not answer is assumed able, which keeps
        # every directly-heated vessel behaving exactly as before.
        if not self._heat_is_available():
            self._stall_anchor_value = value
            self._stall_anchor_at = now
            self._stall_reported = False
            return False

        # Already there. A controller holding a vessel on setpoint produces no
        # rise at all, which is exactly what a dead element looks like to this
        # watch - so proximity to target, not movement, decides. Re-anchor while
        # we are here, so drifting away from target later starts a fresh window
        # rather than inheriting a stale one.
        if target is not None:
            try:
                if float(target) - value <= self.HEAT_STALL_NEAR_TARGET:
                    self._stall_anchor_value = value
                    self._stall_anchor_at = now
                    self._stall_reported = False
                    return False
            except (TypeError, ValueError):
                pass

        if self._stall_anchor_value is None:
            self._stall_anchor_value = value
            self._stall_anchor_at = now
            return False

        if value >= self._stall_anchor_value + self.HEAT_STALL_MIN_RISE:
            self._stall_anchor_value = value
            self._stall_anchor_at = now
            self._stall_reported = False
            return False

        if self._stall_reported:
            return False

        if now - self._stall_anchor_at < self.HEAT_STALL_WINDOW:
            return False

        self._stall_reported = True
        minutes = int(self.HEAT_STALL_WINDOW // 60)
        if target is not None:
            detail = "still {:.1f} below the {:.1f} target".format(
                float(target) - value, float(target)
            )
        else:
            detail = "sitting at {:.1f}".format(value)
        message = (
            "'{}' has not warmed measurably in {} minutes - {}. Check the element, "
            "the SSR and that the probe is actually in the liquid.".format(
                self.name, minutes, detail
            )
        )
        self.logger.warning(message)
        try:
            # Imported here, not at module scope: cbpi.api.dataclasses imports
            # StepState from this module, so a top-level import is a cycle that
            # breaks startup outright.
            from cbpi.api.dataclasses import NotificationAction, NotificationType

            self.cbpi.notify(
                "Heating is not working",
                message,
                NotificationType.WARNING,
                action=[
                    NotificationAction("Keep waiting"),
                    NotificationAction("Stop the profile", self.cbpi.step.stop),
                ],
            )
        except Exception as e:
            self.logger.warning("Could not raise the stalled-heating alert: %s", e)
        return True

    def _done(self, task):
        if self._done_callback is None:
            return
        try:
            result = task.result()
        except asyncio.CancelledError:
            # Stopped deliberately. StepController.stop() owns that transition, so
            # reporting it here as well would fight with it.
            return
        except Exception as e:
            # Previously this only logged, so the controller was never told and the
            # step stayed ACTIVE with a completed, failed task behind it - the same
            # dead end an interrupted restart used to leave. Next then re-awaited
            # the failed task and raised the same exception again.
            self.logger.error(
                "Step %s failed: %s", self.name, e, exc_info=True
            )
            result = StepResult.ERROR
        try:
            self._done_callback(self, result)
        except Exception as e:
            self.logger.error(e)

    async def start(self):
        self.logger.info("Start {}".format(self.name))
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._done)

    async def next(self):
        self.running = False
        self.cancel_reason = StepResult.NEXT
        # A step can be ACTIVE with no task behind it - that is what a profile
        # restored from disk after a restart looks like. Cancelling None raised
        # AttributeError, which surfaced as an HTTP 500 from the Next button and
        # left the profile stuck with no way forward short of editing JSON.
        if self.task is None:
            self.logger.warning(
                "Step %s was asked to advance but has no running task", self.name
            )
            return
        self.task.cancel()
        await self.task

    async def stop(self):
        try:
            self.running = False
            if self.task is not None and self.task.done() is False:
                self.cancel_reason = StepResult.STOP
                self.task.cancel()
                await self.task
        except Exception as e:
            logging.error(e)

    async def reset(self):
        pass

    async def on_props_update(self, props):
        self.props = {**self.props, **props}

    async def save_props(self):
        await self.cbpi.step.save()

    async def push_update(self):
        self.cbpi.step.push_udpate()

    async def on_start(self):
        pass

    async def on_stop(self):
        pass

    async def _run(self):
        try:
            await self.on_start()
            await self.run()
            self.cancel_reason = StepResult.DONE
        except asyncio.CancelledError as e:
            pass
        finally:
            await self.on_stop()

        return self.cancel_reason

    @abstractmethod
    async def run(self):
        pass

    def __str__(self):
        return "name={} props={}, type={}".format(
            self.name, self.props, self.__class__.__name__
        )


class CBPiFermentationStep(CBPiBase):

    def __init__(self, cbpi, fermenter, step, props, on_done) -> None:
        self.fermenter = fermenter
        self.name = step.get("name")
        self.cbpi = cbpi
        self.id = step.get("id")
        self.timer = None
        self._done_callback = on_done
        self.props = props
        self.endtime = int(step.get("endtime"))
        self.cancel_reason: StepResult = None
        self.summary = ""
        self.task = None
        self.running: bool = False
        self.logger = logging.getLogger(__name__)
        self.step = step
        self.update_key = "fermenterstepupdate"

    def _done(self, task):
        if self._done_callback is not None:
            try:
                result = task.result()
                logging.info(result)
                logging.info(self.fermenter.id)
                fermenter = self.fermenter.id
                self._done_callback(self, result, fermenter)
            except Exception as e:
                self.logger.error(e)

    async def start(self):
        self.logger.info("Start {}".format(self.name))
        self.running = True
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._done)

    async def next(self, fermenter=None):
        if fermenter is None:
            self.running = False
            self.cancel_reason = StepResult.NEXT
            self.task.cancel()
            await self.task
        else:
            await self.cbpi.fermenter.next(fermenter)

    async def stop(self):
        try:
            self.running = False
            if self.task is not None and self.task.done() is False:
                self.cancel_reason = StepResult.STOP
                logging.info(self.cancel_reason)
                self.task.cancel()
                await self.task
        except Exception as e:
            logging.error(e)

    async def reset(self):
        pass

    async def on_props_update(self, props):
        self.props = {**self.props, **props}

    async def update_endtime(self):
        await self.cbpi.fermenter.update_endtime(
            self.fermenter.id, self.id, self.endtime
        )

    async def save_props(self):
        self.cbpi.fermenter.save()

    async def push_update(self):
        self.cbpi.fermenter.push_update(self.update_key)

    async def on_start(self):
        pass

    async def on_stop(self):
        pass

    async def _run(self):
        try:
            await self.on_start()
            await self.run()
            self.cancel_reason = StepResult.DONE
        except asyncio.CancelledError as e:
            pass
        finally:
            await self.on_stop()

        return self.cancel_reason

    @abstractmethod
    async def run(self):
        pass

    def __str__(self):
        return "name={} props={}, type={}".format(
            self.name, self.props, self.__class__.__name__
        )
