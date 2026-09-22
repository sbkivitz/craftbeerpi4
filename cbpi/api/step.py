import asyncio
import logging
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
