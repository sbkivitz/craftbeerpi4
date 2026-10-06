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

    # A reading older than this is treated as no reading at all. Sensors publish
    # about once a second, so half a minute is many missed cycles rather than a
    # blip - long enough not to trip on a slow bus, short enough that a heater is
    # not driven against a stale number for any meaningful part of a rest.
    MAX_SENSOR_AGE = 30

    @staticmethod
    def _finite(value):
        """The number in `value`, or None if it is not a usable temperature.

        float() accepts "nan" and "inf" happily, so a non-finite reading used
        to arrive here looking like any other float. That is not a harmless
        oddity in a comparison-driven controller: every comparison against NaN
        is False, so with a NaN reading

            if sensor_value < target_temp - offset_on:      -> False
            elif sensor_value >= target_temp - offset_off:  -> False

        neither branch runs, no command is issued, and the element simply stays
        in whatever state it was already in. Worse, the no-reading path above
        never runs either, so sensor_failures resets to zero and the controller
        goes on believing it has a good reading.

        Measured on an already-energized element: a NaN reading, a NaN target
        and an infinite target each produced NO actuator command at all, with
        the element left on indefinitely.
        """
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            return None
        return numeric

    def _read_temp(self, sensor_id):
        """Current temperature, or None if it cannot be trusted.

        get_sensor_value() returns None for a missing or failing sensor, so the
        previous `.get("value")` raised AttributeError and killed the control
        task for the rest of the brew.

        A value alone is not enough. Several sensor implementations - the bundled
        OneWire one among them - catch a read error and keep publishing their last
        reading, so an unplugged probe reports a plausible temperature forever. A
        reading that has stopped being updated is therefore rejected here, which
        the None check on its own could never catch.
        """
        try:
            state = self.get_sensor_value(sensor_id)
            value = self._finite(state.get("value"))
        except (AttributeError, TypeError, ValueError):
            return None
        if value is None:
            return None

        age = state.get("age")
        # The sensor's own cadence, not a fixed number of seconds. A OneWire
        # probe on its default 60s interval is legitimately 59s old, and
        # cutting heat on that cycles the element every minute of a brew day
        # with nothing wrong. See SensorController.expected_max_age().
        limit = state.get("max_age") or self.MAX_SENSOR_AGE
        if age is not None and age > limit:
            logging.warning(
                "Hysteresis: ignoring sensor %s, last updated %.0fs ago (limit %.0fs)",
                sensor_id,
                age,
                limit,
            )
            return None

        return value

    async def run(self):
        try:
            # Resolve the actuator before anything that can fail.
            #
            # float(self.props.get("OffsetOn", 0)) raises on a malformed
            # offset, and the finally below commands self.heater - which would
            # not exist yet. Same correction as PIDHerms and PIDBoil: resolving
            # the actuator is what makes the cleanup able to act at all.
            self.kettle = self.get_kettle(self.id)
            self.heater = self.kettle.heater
            heater = self.cbpi.actor.find_by_id(self.heater)

            # Offsets go through the same finiteness check. A NaN offset is not
            # a harmless configuration error: target_temp - nan is nan, so both
            # comparisons below go False and the controller stops commanding
            # the element entirely, exactly as a NaN reading would.
            self.offset_on = self._finite(self.props.get("OffsetOn", 0))
            self.offset_off = self._finite(self.props.get("OffsetOff", 0))
            if self.offset_on is None or self.offset_off is None:
                self.offset_on = 0 if self.offset_on is None else self.offset_on
                self.offset_off = 0 if self.offset_off is None else self.offset_off
                self.cbpi.notify(
                    "{} config".format(getattr(self.kettle, "name", "Kettle")),
                    "Hysteresis offset was not a usable number - using 0",
                    NotificationType.WARNING,
                )
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
                # The target gets the same treatment as the reading. A NaN or
                # infinite setpoint makes every comparison below False, which
                # leaves the element in whatever state it was already in with
                # no command issued and no fault raised.
                target_temp = self._finite(self.get_kettle_target_temp(self.id))
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
                        reason = (
                            "No temperature reading"
                            if sensor_value is None
                            else "Target temperature is not a usable number"
                        )
                        self.cbpi.notify(
                            "{} sensor".format(getattr(self.kettle, "name", "Kettle")),
                            "{} - heating suspended until it returns".format(reason),
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
