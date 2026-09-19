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
            return state
        except Exception as e:
            logging.error("Failed read sensor value {} {} ".format(id, e))
            return None

    def is_fresh(self, id, max_age=30):
        """True when this sensor produced a reading within max_age seconds.

        Reading a value tells you nothing about whether the probe is still alive.
        Several sensor implementations - the bundled OneWire one among them - catch
        a read error and keep publishing their last value, so a disconnected probe
        reports a plausible temperature indefinitely. Callers that are about to act
        on a reading, especially to energize a heater, should ask this first.

        Unknown sensors, sensors that have never produced a reading, and sensors
        whose instance cannot be reached all answer False: the safe direction is to
        treat anything unproven as stale.
        """
        if id is None:
            return False
        try:
            last_update = getattr(self.find_by_id(id).instance, "last_update", None)
        except Exception:
            return False
        if last_update is None:
            return False
        return (time.time() - last_update) <= max_age
