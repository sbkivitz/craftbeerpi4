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
            props_by_type=self._stored_settings(data.get("props_by_type")),
        )

    @staticmethod
    def _stored_settings(value):
        """{logic name: settings} as read from kettle.json, or {} if unusable.

        A file written before this existed has no such key, and a hand-edited
        one may hold anything. Neither may stop the kettle loading.
        """
        if not isinstance(value, dict):
            return {}
        return {
            str(logic): dict(settings)
            for logic, settings in value.items()
            if isinstance(settings, dict)
        }

    async def update(self, item):
        """Save an edited kettle, keeping each logic's settings to itself.

        A kettle's props are its logic's configuration, and two logics can
        declare settings of the same name that mean different things. On 25 Oct
        2026 the HLT was switched from PID_HERMS to PIDBoil and kept every
        PID_HERMS setting: PIDBoil then read PID_HERMS's P 2 / I 0.005 - the
        mash loop's gains, in degrees of HLT offset - as gains for the element.
        Its integral wound up during the heat-up, it was still asking for 90%
        at the 152 F target, and the brewer stopped it at 156 F.

        So when the logic changes, the old logic's settings are put aside in
        props_by_type, where switching back finds them, and a brewer whose new
        logic arrives still holding the old logic's values is told which ones.
        The new settings themselves are saved as sent: whether a value was kept
        on purpose is the brewer's call, and the interface now starts a logic
        from its own settings rather than another's.
        """
        old = self.find_by_id(item.id)
        if old is not None:
            try:
                item.props_by_type = self._settings_by_logic(old, item)
                self._warn_if_settings_carried_over(old, item)
            except Exception as e:  # noqa: BLE001 - an edit must never fail on this
                logging.warning("Kettle %s: could not keep logic settings: %s", item.id, e)
        return await super().update(item)

    @staticmethod
    def _settings_by_logic(old, item):
        kept = KettleController._stored_settings(getattr(old, "props_by_type", None))
        if old.type and item.type != old.type:
            kept[old.type] = old.props.to_dict()
        # The active logic's settings live in props; a stale copy here would be
        # what switching away and back restored instead of the latest.
        kept.pop(item.type, None)
        return kept

    def _warn_if_settings_carried_over(self, old, item):
        """Tell the brewer when the new logic arrived holding the old one's settings.

        The signature is settings only the OLD logic declares still being in
        the new save - HLT_P arriving with PIDBoil - which happens only when
        the old set was carried over wholesale: an interface from before
        props_by_type, or an API client. The fixed interface starts a logic
        from its own settings, so re-entering a value that happens to equal
        the old one (D 0, Max_Output 100) does not trip this. A warning that
        fires on correct input is one the brewer learns to ignore.
        """
        if not old.type or not item.type or item.type == old.type:
            return
        new_labels = self._declared(item.type)
        old_only = set(self._declared(old.type)) - set(new_labels)
        before, after = old.props.to_dict(), item.props.to_dict()
        if not any(label in after for label in old_only):
            return
        carried = [
            label for label in new_labels
            if label in before and label in after
            and before[label] not in (None, "") and after[label] == before[label]
        ]
        if not carried:
            return
        message = (
            "'{}' changed from {} to {} still holding {}'s settings: {}. The "
            "same setting can mean something else in {} - check them before "
            "starting it.".format(
                getattr(item, "name", item.id), old.type, item.type, old.type,
                ", ".join("{} {}".format(label, after[label]) for label in carried),
                item.type,
            )
        )
        logging.warning("Kettle: %s", message)
        try:
            from cbpi.api.dataclasses import NotificationType

            self.cbpi.notify("Logic changed", message, NotificationType.WARNING)
        except Exception:  # noqa: BLE001
            self.cbpi.notify("Logic changed", message)

    def _declared(self, logic):
        """The setting names a registered logic declares, in its own order."""
        labels = []
        for p in (self.types.get(logic) or {}).get("properties") or []:
            label = p.get("label") if isinstance(p, dict) else None
            if label and label not in labels:
                labels.append(label)
        return labels

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
