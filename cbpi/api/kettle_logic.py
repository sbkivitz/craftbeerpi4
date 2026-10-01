import asyncio
import logging
from abc import ABCMeta

from cbpi.api.base import CBPiBase
from cbpi.api.extension import CBPiExtension


class CBPiKettleLogic(CBPiBase, metaclass=ABCMeta):

    def __init__(self, cbpi, id, props):
        self.cbpi = cbpi
        self.id = id
        self.props = props
        self.state = False
        self.running = False

    def init(self):
        pass

    async def on_start(self):
        pass

    async def on_stop(self):
        pass

    async def run(self):
        pass

    async def _run(self):

        try:
            await self.on_start()
            self.cancel_reason = await self.run()
        except asyncio.CancelledError as e:
            pass
        finally:
            await self.on_stop()

    def get_state(self):
        return dict(running=self.state)

    def log_data(self, series, value):
        """Write a control signal to the log store, under its own series name.

        Kettle logics had no way to log anything. CBpiActor and CBpiSensor both
        have log_data(); this class did not, which is why no plugin has ever
        recorded a control signal and why a PID here cannot be evaluated after
        the fact. You can see the temperature it produced, but not the duty it
        asked for - and those two curves are what separate "badly tuned" from
        "undersized element" from "laggy probe", which look alike from the
        temperature alone.

        `series` is appended to this kettle's id, so one logic can record
        several signals - duty, setpoint, and for a cascade an inner pair -
        without colliding with the kettle's own sensor. The resulting name is
        just an id as far as the log store is concerned, so the existing
        rotation, batching, retention and charting all apply with no new
        plumbing: /log/<kettleid>.<series> returns it like any sensor.

        Deliberately not gated here. Whether to log at all is a policy question
        that belongs to the caller, which knows whether the brewer asked for
        it; a base class that silently dropped writes would be worse than one
        that just writes.
        """
        try:
            self.cbpi.log.log_data("{}.{}".format(self.id, series), value)
        except Exception as e:  # noqa: BLE001 - telemetry never breaks control
            logging.getLogger(type(self).__name__).warning(
                "Could not log %s for %s: %s", series, self.id, e
            )

    def pid_logging_enabled(self):
        """Has the brewer asked for control signals to be recorded?

        Read every call rather than cached, so switching it on mid-brew starts
        recording without a restart - which is the point of a knob. It is a
        dictionary lookup against the config cache, not a file read.
        """
        try:
            return str(self.cbpi.config.get("PID_LOGGING", "No")) == "Yes"
        except Exception:  # noqa: BLE001
            return False

    async def start(self):

        self.state = True

    async def stop(self):

        self.task.cancel()
        await self.task
        self.state = False
