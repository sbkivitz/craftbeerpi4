import asyncio
import logging
from asyncio import tasks

from cbpi.api import *
from cbpi.api.dataclasses import NotificationType


@parameters(
    [
        Property.Number(
            label="OffsetOn",
            configurable=True,
            description="Offset below target temp when heater should switched on",
        ),
        Property.Number(
            label="OffsetOff",
            configurable=True,
            description="Offset below target temp when heater should switched off",
        ),
    ]
)
class Hysteresis(CBPiKettleLogic):

    # Consecutive unreadable samples tolerated before the user is told. At one
    # sample per second this rides out a brief 1-wire glitch without nagging.
    MAX_SENSOR_FAILURES = 5

    def _read_temp(self, sensor_id):
        """Current temperature, or None if it cannot be read.

        get_sensor_value() returns None for a missing or failing sensor, so the
        previous `.get("value")` raised AttributeError and killed the control
        task for the rest of the brew.
        """
        try:
            value = self.get_sensor_value(sensor_id).get("value")
            return float(value)
        except (AttributeError, TypeError, ValueError):
            return None

    async def run(self):
        try:
            self.offset_on = float(self.props.get("OffsetOn", 0))
            self.offset_off = float(self.props.get("OffsetOff", 0))
            self.kettle = self.get_kettle(self.id)
            self.heater = self.kettle.heater
            heater = self.cbpi.actor.find_by_id(self.heater)
            logging.info(
                "Hysteresis {} {} {} {}".format(
                    self.offset_on, self.offset_off, self.id, self.heater
                )
            )

            # self.get_actor_state()

            sensor_failures = 0
            fault_notified = False

            while self.running == True:

                sensor_value = self._read_temp(self.kettle.sensor)
                target_temp = self.get_kettle_target_temp(self.id)
                try:
                    heater_state = heater.instance.state
                except:
                    heater_state = False

                if sensor_value is None or target_temp is None:
                    # Cannot know the temperature, so do not heat - but stay alive.
                    # Exiting here used to end temperature control for the rest of
                    # the brew after a single bad read, silently.
                    sensor_failures += 1
                    if self.heater and (heater_state == True):
                        await self.actor_off(self.heater)
                    if sensor_failures >= self.MAX_SENSOR_FAILURES and not fault_notified:
                        fault_notified = True
                        self.cbpi.notify(
                            "{} sensor".format(getattr(self.kettle, "name", "Kettle")),
                            "No temperature reading - heating suspended until it returns",
                            NotificationType.ERROR,
                        )
                    await asyncio.sleep(1)
                    continue

                if fault_notified:
                    self.cbpi.notify(
                        "{} sensor".format(getattr(self.kettle, "name", "Kettle")),
                        "Temperature reading restored - heating resumed",
                        NotificationType.INFO,
                    )
                sensor_failures = 0
                fault_notified = False

                if sensor_value < target_temp - self.offset_on:
                    if self.heater and (heater_state == False):
                        await self.actor_on(self.heater)
                elif sensor_value >= target_temp - self.offset_off:
                    if self.heater and (heater_state == True):
                        await self.actor_off(self.heater)
                await asyncio.sleep(1)

        except asyncio.CancelledError as e:
            pass
        except Exception as e:
            logging.error("CustomLogic Error {}".format(e))
        finally:
            self.running = False
            await self.actor_off(self.heater)


def setup(cbpi):
    """
    This method is called by the server during startup
    Here you need to register your plugins at the server

    :param cbpi: the cbpi core
    :return:
    """

    cbpi.plugin.register("Hysteresis", Hysteresis)
