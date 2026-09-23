import logging

from cbpi.api.dataclasses import Actor, Props
from cbpi.controller.basic_controller2 import BasicController
from tabulate import tabulate


class ActorController(BasicController):

    def __init__(self, cbpi):
        super(ActorController, self).__init__(cbpi, Actor, "actor.json")
        self.update_key = "actorupdate"
        self.sorting = True

    def create(self, data):
        # The base implementation only restores id/name/type/props, so everything
        # actor.json persists beyond that was silently reset to the dataclass
        # defaults on every start - a heater configured to run at 70% came back at
        # 100%. to_dict() writes these fields, so read them back.
        #
        # 'state' is deliberately NOT restored. It is written so the UI can show
        # what an actor was doing, but restoring it would switch a heater or pump
        # back on by itself after a restart or a power cut. Actors always come up
        # off, and something has to ask for them explicitly.
        return Actor(
            data.get("id"),
            data.get("name"),
            type=data.get("type"),
            props=Props(data.get("props", {})),
            power=data.get("power", 100),
            maxoutput=data.get("maxoutput", 100),
            output=data.get("output", data.get("maxoutput", 100)),
            timer=data.get("timer", 0),
        )

    async def shutdown(self, app=None):
        """Switch every actor off before the process exits.

        The base implementation only cancels each actor's asyncio task. For a GPIO
        actor that task is a PWM duty loop which drives the pin HIGH and then
        awaits (gpioactor run()); cancelling it mid-cycle leaves the pin HIGH.
        Nothing calls GPIO.cleanup() either, and RPi.GPIO does not reset pins when
        the process exits - so stopping the service with a heater on left a
        kilowatt element energized indefinitely with nothing supervising it.

        Actors are switched off first, so the duty loop sees state False and stops
        driving the pin, and again after the tasks are cancelled, so a loop that
        was mid-cycle cannot leave the output latched on.
        """
        for item in self.data:
            try:
                if item.instance is not None:
                    await item.instance.off()
            except Exception as e:
                logging.error("Failed to switch off actor %s during shutdown: %s", item.id, e)

        try:
            await super().shutdown(app)
        finally:
            # Always re-confirm, even if task cancellation raised: leaving an
            # element energized is worse than a noisy shutdown.
            for item in self.data:
                try:
                    if item.instance is not None:
                        await item.instance.off()
                except Exception as e:
                    logging.error("Failed to confirm actor %s off during shutdown: %s", item.id, e)

    async def on(self, id, power=None, output=None):
        try:
            item = self.find_by_id(id)
            if power is None:
                logging.info("Power is none")
                if item.power:
                    power = item.power
                else:
                    power = 100

            if output is None:
                logging.info("Output is none")
                if item.output:
                    output = item.output
                else:
                    output = 100
            if item.instance.state is False:
                try:
                    await item.instance.on(power, output)
                except:
                    await item.instance.on(power)
                # Record what was actually commanded.
                #
                # to_dict() reports state from the live instance but power from
                # this dataclass, and nothing here ever wrote to it - so the
                # power shown in the interface, and read back by any logic that
                # inspects the actor, was whatever the actor happened to be
                # created with. A boil kettle driven at 100% then 85% reported
                # 70% throughout, and `on(id, power=None)` resumed at that stale
                # figure rather than the last one asked for.
                item.power = self._clamp_power(power)
                item.output = output
                # await self.push_udpate()
                self.cbpi.ws.send(
                    dict(
                        topic=self.update_key,
                        data=list(map(lambda item: item.to_dict(), self.data)),
                    ),
                    self.sorting,
                )
                self.cbpi.push_update(
                    "cbpi/actorupdate/{}".format(id), item.to_dict(), True
                )
            else:
                await self.set_power(id, power)
                await self.set_output(id, output)
            return True
        except Exception as e:
            logging.error("Failed to switch on Actor {} {}".format(id, e))
            return False

    async def off(self, id):
        try:
            item = self.find_by_id(id)
            if item.instance.state is True:
                await item.instance.off()
                # await self.push_udpate()
                self.cbpi.ws.send(
                    dict(
                        topic=self.update_key,
                        data=list(map(lambda item: item.to_dict(), self.data)),
                    ),
                    self.sorting,
                )
                self.cbpi.push_update("cbpi/actorupdate/{}".format(id), item.to_dict())
            return True
        except Exception as e:
            logging.error("Failed to switch on Actor {} {}".format(id, e), True)
            return False

    async def toogle(self, id):
        try:
            item = self.find_by_id(id)
            instance = item.get("instance")
            await instance.toggle()
            self.cbpi.ws.send(
                dict(
                    topic=self.update_key,
                    data=list(map(lambda item: item.to_dict(), self.data)),
                ),
                self.sorting,
            )
            self.cbpi.push_update("cbpi/actorupdate/{}".format(id), item.to_dict())
        except Exception as e:
            logging.error("Failed to toggle Actor {} {}".format(id, e))

    @staticmethod
    def _clamp_power(power):
        """A percentage, and nothing else. Out of range means the caller is
        confused, and driving an element from a confused number is worse than
        driving it from a sane one."""
        try:
            power = int(round(float(power)))
        except (TypeError, ValueError):
            return 100
        return max(0, min(100, power))

    async def set_power(self, id, power):
        try:
            item = self.find_by_id(id)
            await item.instance.set_power(power)
            # See on(): this is the field the interface and other logics read.
            item.power = self._clamp_power(power)
            output = round(item.maxoutput * item.power / 100)
            if item.output != output:
                item.output = output

        except Exception as e:
            logging.error("Failed to set power {} {}".format(id, e))

    async def set_output(self, id, output):
        try:
            item = self.find_by_id(id)
            await item.instance.set_output(output)
            if item.output != output:
                item.output = output
                power = round(output / item.maxoutput * 100)
                if item.power != power:
                    item.power = self._clamp_power(power)
                    await item.instance.set_power(item.power)
        except Exception as e:
            logging.error("Failed to set output {} {}".format(id, e))

    async def actor_update(self, id, power, output=None, maxoutput=None):
        try:
            item = self.find_by_id(id)
            if maxoutput:
                item.maxoutput = maxoutput
            item.power = round(power)
            if output:
                item.output = round(output)

            # await self.push_udpate()
            self.cbpi.ws.send(
                dict(
                    topic=self.update_key,
                    data=list(map(lambda item: item.to_dict(), self.data)),
                ),
                self.sorting,
            )
            self.cbpi.push_update("cbpi/actorupdate/{}".format(id), item.to_dict())
        except Exception as e:
            logging.error("Failed to update Actor {} {}".format(id, e))

    async def timeractor_update(self, id, timer):
        try:
            item = self.find_by_id(id)
            item.timer = round(timer)
            # await self.push_udpate()
            self.cbpi.ws.send(
                dict(
                    topic=self.update_key,
                    data=list(map(lambda item: item.to_dict(), self.data)),
                )
            )
            self.cbpi.push_update("cbpi/actorupdate/{}".format(id), item.to_dict())
        except Exception as e:
            logging.error("Failed to update Actor {} {}".format(id, e))

    async def ws_actor_update(self):
        try:
            self.cbpi.ws.send(
                dict(
                    topic=self.update_key,
                    data=list(map(lambda x: x.to_dict(), self.data)),
                ),
                self.sorting,
            )
            return True
        except Exception as e:
            logging.error("Failed to update Actors {}".format(e))
            return False
