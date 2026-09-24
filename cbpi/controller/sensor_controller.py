import logging
import time

from cbpi.api.dataclasses import Sensor
from cbpi.controller.basic_controller2 import BasicController


class SensorController(BasicController):
    def __init__(self, cbpi):
        super(SensorController, self).__init__(cbpi, Sensor, "sensor.json")
        self.update_key = "sensorupdate"
        self.sorting = True

    def create_dict(self, data):
        try:
            instance = data.get("instance")
            state = instance.get_state()
        except Exception as e:
            logging.error("Failed to create sensor dict {} ".format(e))
            state = dict()

        return dict(
            name=data.get("name"),
            id=data.get("id"),
            type=data.get("type"),
            state=state,
            props=data.get("props", []),
        )

    def get_sensor_value(self, id):
        if id is None:
            return None
        try:
            instance = self.find_by_id(id).instance
            state = instance.get_state()
            if not isinstance(state, dict):
                return state
            # Copied so a sensor returning its own internal dict is not mutated.
            state = dict(state)
            last_update = getattr(instance, "last_update", None)
            state.setdefault("timestamp", last_update)
            state.setdefault(
                "age", None if last_update is None else time.time() - last_update
            )
            # How old this particular sensor's reading is allowed to get, so a
            # consumer does not have to know anything about publish intervals.
            state.setdefault("max_age", self.expected_max_age(id))
            return state
        except Exception as e:
            logging.error("Failed read sensor value {} {} ".format(id, e))
            return None

    # A sensor that publishes once a minute is not stale at 31 seconds.
    #
    # Thirty seconds was chosen on the assumption that drivers publish about
    # once a second. The bundled OneWire driver defaults to `Interval = 60`, so
    # on a healthy probe the reported age sawtooths 0 -> 60 and spends roughly
    # half of every minute above 30. A consumer that cuts heat on that cycles
    # the element and raises a fault pair every minute of a brew day, with
    # nothing actually wrong. MQTT is worse: Tasmota's default telemetry period
    # is 300s, so such a sensor would never look fresh at all.
    #
    # OneWire's own internal check already had the right idea - it allows
    # `interval * 3` - so this follows that convention rather than inventing a
    # second one. Three missed publications is a real fault at any cadence,
    # where a fixed wall-clock number is only right for one.
    DEFAULT_MAX_AGE = 30
    MISSED_PUBLICATIONS = 3

    def expected_max_age(self, id, floor=None):
        """How old a reading from this sensor may be before it is suspect.

        `floor` seconds, or three of the sensor's own publish intervals,
        whichever is longer. A sensor that declares no interval just gets the
        floor.
        """
        if floor is None:
            floor = self.DEFAULT_MAX_AGE
        try:
            props = getattr(self.find_by_id(id).instance, "props", None) or {}
            interval = props.get("Interval", None)
            if interval is None:
                return floor
            return max(floor, float(interval) * self.MISSED_PUBLICATIONS)
        except Exception:  # noqa: BLE001
            return floor

    def is_fresh(self, id, max_age=None):
        """True when this sensor produced a reading recently enough.

        Reading a value tells you nothing about whether the probe is still alive.
        Several sensor implementations - the bundled OneWire one among them - catch
        a read error and keep publishing their last value, so a disconnected probe
        reports a plausible temperature indefinitely. Callers that are about to act
        on a reading, especially to energize a heater, should ask this first.

        `max_age` defaults to the sensor's own publish cadence rather than a
        fixed number of seconds - see expected_max_age().

        Unknown sensors, sensors that have never produced a reading, and sensors
        whose instance cannot be reached all answer False: the safe direction is to
        treat anything unproven as stale.
        """
        if id is None:
            return False
        if max_age is None:
            max_age = self.expected_max_age(id)
        try:
            last_update = getattr(self.find_by_id(id).instance, "last_update", None)
        except Exception:
            return False
        if last_update is None:
            return False
        return (time.time() - last_update) <= max_age
