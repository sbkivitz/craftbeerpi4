import asyncio
from abc import ABCMeta

from cbpi.api.config import ConfigType

__all__ = ["CBPiActor"]

import logging

logger = logging.getLogger(__file__)


class CBPiActor(metaclass=ABCMeta):

    def __init__(self, cbpi, id, props):
        self.cbpi = cbpi
        self.id = id
        self.props = props
        self.logger = logging.getLogger(__file__)
        self.data_logger = None
        self.state = False
        self.running = False
        self.power = 100
        self.output = 100
        self.maxoutput = 100
        self.timer = 0

    def init(self):
        pass

    def log_data(self, value):
        self.cbpi.log.log_data(self.id, value)

    def get_state(self):
        return dict(state=self.state)

    async def start(self):
        pass

    async def stop(self):
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

    def get_static_config_value(self, name, default):
        return self.cbpi.static_config.get(name, default)

    def get_config_value(self, name, default):
        return self.cbpi.config.get(name, default=default)

    async def set_config_value(self, name, value):
        return await self.cbpi.config.set(name, value)

    async def add_config_value(
        self, name, value, type: ConfigType, description, options=None
    ):
        await self.cbpi.add(name, value, type, description, options=None)

    async def on(self, power, output=None):
        """
        Code to switch the actor on. Power is provided as integer value

        :param power: power value between 0 and 100
        :return: None
        """
        pass

    async def off(self):
        """
        Code to switch the actor off

        :return: None
        """
        pass

    async def set_power(self, power):
        """
        Set this actor's power level, as a percentage.

        The base implementation records the value so anything reading back
        `instance.power` - a PWM loop, a simulated vessel, the UI - sees what was
        actually asked for. It previously returned the *existing* power without
        assigning anything, with unreachable code after the return, so an actor
        that did not override this silently ignored every power command: a heater
        told to run at 30% stayed at whatever it was, usually 100.

        Hardware actors still override this to drive the output. Clamped to the
        actor's own ceiling, because a caller asking for more than maxoutput is
        asking for something the device cannot do.

        :return: dict power
        """
        try:
            value = float(power)
        except (TypeError, ValueError):
            self.logger.warning(
                "Actor %s ignoring non-numeric power %r", self.id, power
            )
            return dict(power=self.power)
        ceiling = self.maxoutput if self.maxoutput is not None else 100
        self.power = max(0, min(value, ceiling))
        return dict(power=self.power)

    async def set_output(self, output):
        """
        Code to set power for actor

        :return: dict power
        """
        return dict(output=self.output)
        pass