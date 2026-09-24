import logging

from cbpi.api.dataclasses import Kettle, Props
from cbpi.controller.basic_controller2 import BasicController
from tabulate import tabulate


class KettleController(BasicController):

    def __init__(self, cbpi):
        super(KettleController, self).__init__(cbpi, Kettle, "kettle.json")
        self.update_key = "kettleupdate"
        self.autostart = False

    def create(self, data):
        return Kettle(
            data.get("id"),
            data.get("name"),
            type=data.get("type"),
            props=Props(data.get("props", {})),
            sensor=data.get("sensor"),
            heater=data.get("heater"),
            agitator=data.get("agitator"),
            target_temp=data.get("target_temp", 0),
        )

    async def start(self, id):
        # Two kettle logics driving one heater fight each other.
        #
        # On a HERMS it is normal and correct for two kettles to name the same
        # heating element: the HLT element heats the mash through a coil, so the
        # HLT and the mash tun genuinely share it. What is not correct is for
        # both logics to be RUNNING at once. PID_HERMS drives that element from
        # the mash temperature while Hysteresis drives it from the HLT's own
        # target, and every second one of them undoes the other.
        #
        # The dangerous direction is specific: the HLT logic holds its own
        # setpoint, which may be left over from an earlier step, so it can hold
        # the element on while the cascade is trying to ease it off - and the
        # mash overshoots.
        #
        # Warned rather than blocked. There is no legitimate reason to run both,
        # but refusing to start a kettle the brewer explicitly asked for is a
        # surprising way to find that out, and the brewer may be doing something
        # deliberate that this cannot see.
        try:
            await self._warn_if_heater_shared(id)
        except Exception as e:  # noqa: BLE001
            logging.debug("shared-heater check skipped: %s", e)
        await super().start(id)

    async def _warn_if_heater_shared(self, id):
        item = self.find_by_id(id)
        heater = getattr(item, "heater", None)
        if not heater:
            return
        clashes = []
        for other in self.data:
            if other.id == item.id or getattr(other, "heater", None) != heater:
                continue
            instance = getattr(other, "instance", None)
            if instance is not None and getattr(instance, "running", False):
                clashes.append(other)
        if not clashes:
            return

        names = ", ".join(getattr(k, "name", k.id) for k in clashes)
        message = (
            "'{}' and '{}' drive the same heater. Both logics are now running "
            "and will fight over it - one holding the element on while the "
            "other tries to ease it off. Stop whichever one you do not want "
            "controlling it.".format(getattr(item, "name", id), names)
        )
        logging.warning("Kettle: %s", message)
        try:
            from cbpi.api.dataclasses import NotificationType

            self.cbpi.notify("Shared heater", message, NotificationType.WARNING)
        except Exception:  # noqa: BLE001
            self.cbpi.notify("Shared heater", message)

    async def toggle(self, id):

        try:
            item = self.find_by_id(id)

            if item.instance is None or item.instance.state == False:
                await self.start(id)
            else:
                await item.instance.stop()
            await self.push_udpate()
        except Exception as e:
            logging.error("Failed to switch on KettleLogic {} {}".format(id, e))

    async def set_target_temp(self, id, target_temp):
        try:
            item = self.find_by_id(id)
            item.target_temp = target_temp
            await self.save()
        except Exception as e:
            logging.error("Failed to set Target Temp {} {}".format(id, e))

    async def stop(self, id):
        try:
            logging.info("Stop Kettle {}".format(id))
            item = self.find_by_id(id)
            if item.instance:
                await item.instance.stop()
            await self.push_udpate()
        except Exception as e:
            logging.error("Failed to switch off KettleLogic {} {}".format(id, e))
