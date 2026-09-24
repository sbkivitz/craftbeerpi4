import asyncio
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

    INTERLOCK_SETTING = "ACTOR_INTERLOCK_GROUPS"
    INTERLOCK_MODE = "ACTOR_INTERLOCK_MODE"
    MODE_OFF = "Off"
    MODE_ONE_ELEMENT = "One heating element at a time"

    def _interlock_lock(self):
        """Serializes the check-and-commit in on(). Created on first use
        because this controller is not constructed through __init__."""
        lock = getattr(self, "_interlock_lock_obj", None)
        if lock is None:
            lock = asyncio.Lock()
            self._interlock_lock_obj = lock
        return lock

    def _heater_actor_ids(self):
        """The actors that kettles drive as heating elements.

        These are the multi-kilowatt loads, and the software already knows
        which they are - so "one element at a time" needs nothing typed and
        cannot be broken by renaming an actor.

        Fermenter heaters are deliberately excluded. They are tens of watts and
        interlocking a fermentation chamber against a boil kettle would stop
        fermentation temperature control for the length of a brew day.
        """
        ids = set()
        try:
            for kettle in self.cbpi.kettle.data:
                heater = getattr(kettle, "heater", None)
                if heater:
                    ids.add(heater)
        except Exception:  # noqa: BLE001
            return set()
        return ids

    def _interlock_groups(self):
        """Sets of actor *ids* that must never be energized together.

        Two sources, both read on every call so a change takes effect without
        a restart.

        The first is ACTOR_INTERLOCK_MODE, a plain choice:

            Off                            - no interlock at all (default)
            One heating element at a time  - every kettle heater is exclusive

        The second is the legacy ACTOR_INTERLOCK_GROUPS string, kept working
        for anyone already relying on it: groups separated by semicolons, actor
        names separated by commas. Naming actors in free text is a poor
        interface - a typo fails open, and renaming an actor silently breaks
        the interlock - so the mode above is the supported way to do this and
        this is only a fallback. Names are resolved to ids here so that the
        rest of the logic never compares names.

        **Off by default, which means no interlock at all.** That default is
        the important part. Plenty of rigs are wired for a 50A supply and heat
        two elements deliberately; a safety feature that silently changes what
        a working rig does is not a safety feature.
        """
        groups = []

        try:
            mode = self.cbpi.config.get(self.INTERLOCK_MODE, self.MODE_OFF)
        except Exception:  # noqa: BLE001
            mode = self.MODE_OFF
        if str(mode).strip() == self.MODE_ONE_ELEMENT:
            heaters = self._heater_actor_ids()
            # A group of one interlocks with nothing.
            if len(heaters) > 1:
                groups.append(heaters)

        try:
            raw = self.cbpi.config.get(self.INTERLOCK_SETTING, "") or ""
        except Exception:  # noqa: BLE001
            raw = ""
        by_name = {}
        for actor in self.data:
            if actor.name:
                by_name.setdefault(str(actor.name).strip(), actor.id)
        for chunk in str(raw).split(";"):
            ids = set()
            for part in chunk.split(","):
                part = part.strip()
                if not part:
                    continue
                resolved = by_name.get(part)
                if resolved is None:
                    logging.warning(
                        "Actor interlock: no actor named %r - that group member "
                        "is being ignored, which means the interlock is not "
                        "protecting it",
                        part,
                    )
                    continue
                ids.add(resolved)
            if len(ids) > 1:
                groups.append(ids)

        return groups

    def _interlock_conflict(self, item):
        """An actor already on that `item` may not be on at the same time."""
        groups = self._interlock_groups()
        if not groups:
            return None
        for group in groups:
            if item.id not in group:
                continue
            for other in self.data:
                if other.id == item.id or other.id not in group:
                    continue
                try:
                    if other.instance is not None and other.instance.state is True:
                        return other
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def on(self, id, power=None, output=None):
        try:
            item = self.find_by_id(id)
            # Remember which of the two the caller actually asked for. Power and
            # output are two views of one quantity - output is maxoutput * power
            # / 100 - so setting both independently is incoherent, and setting
            # both from independently defaulted values is a bug. See below.
            power_given = power is not None
            output_given = output is not None
            if power is None:
                # `is not None`, not truthiness.
                #
                # Zero is a legitimate commanded level, not "unset". Treating it
                # as unset made a deliberate 0 fall through to 100, so an actor
                # that had been ramped down to zero came back at FULL power the
                # next time anything called on() without an explicit level.
                # The dataclass already defaults power to 100, so a fresh actor
                # still starts fully on - nothing needs truthiness to get that.
                if item.power is not None:
                    power = item.power
                else:
                    power = 100

            if output is None:
                if item.output is not None:
                    output = item.output
                else:
                    output = 100
            if item.instance.state is False:
                # Interlock, checked here because every element in the system is
                # energized through this one function - no step, logic or
                # third-party plugin can go round it.
                #
                # Only on the off -> on transition: on() doubles as "set power"
                # for an actor that is already running, and that must not look
                # like a second element trying to start.
                #
                # Refused rather than granted by switching the other one off.
                # Turning off another vessel's element behind the brewer's back
                # is a worse failure than declining to start this one, and it
                # would do it silently.
                #
                # Check and commit happen under one lock. Testing for a
                # conflict and then energizing as two separate steps is a
                # check-then-act race: two concurrent starts - two steps, a
                # step and the interface, a step and MQTT - can both find the
                # other element off and both proceed. The window is small, but
                # software doing two things at once is the entire case this
                # exists to prevent. The notification is raised outside the
                # lock because it is slow and does not need protecting.
                async with self._interlock_lock():
                    blocking = self._interlock_conflict(item)
                    if blocking is None:
                        try:
                            await item.instance.on(power, output)
                        except:
                            await item.instance.on(power)

                if blocking is not None:
                    message = (
                        "'{}' was not switched on: '{}' is already running and "
                        "they are interlocked. Switch that off first, or set "
                        "{} to '{}' if this rig can supply both.".format(
                            item.name,
                            blocking.name,
                            self.INTERLOCK_MODE,
                            self.MODE_OFF,
                        )
                    )
                    logging.warning("Actor interlock: %s", message)
                    try:
                        from cbpi.api.dataclasses import NotificationType

                        self.cbpi.notify(
                            "Interlock", message, NotificationType.WARNING
                        )
                    except Exception:  # noqa: BLE001
                        self.cbpi.notify("Interlock", message)
                    return False
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
                # Already running, so this is a change of level rather than a
                # start. Exactly one of the two drives; the other is derived.
                #
                # It used to call both. `output` was defaulted from the actor
                # before `set_power` ran, so `set_output` then recomputed the
                # power from that stale figure and undid the change - asking a
                # running heater for 60% left it at 100%. It was invisible only
                # because item.power was never written at all, which made the
                # guard inside set_output compare two stale values and do
                # nothing.
                if output_given and not power_given:
                    await self.set_output(id, output)
                else:
                    await self.set_power(id, power)
            return True
        except Exception as e:
            logging.error("Failed to switch on Actor {} {}".format(id, e))
            return False

    async def off(self, id):
        try:
            item = self.find_by_id(id)
            if item is None:
                logging.error("Cannot switch off unknown actor %s", id)
                return False

            # Unconditional, and idempotent.
            #
            # This used to call the hardware only when the software believed the
            # actor was already on. That makes OFF a no-op exactly when it is
            # most needed: after a desync, a restart, a relay that latched, or a
            # failed ON that left the instance's state flag behind. An emergency
            # stop would then return success having sent nothing at all.
            #
            # Switching off something already off costs one redundant command
            # and is always safe. Not switching off something that is on is not.
            await item.instance.off()

            self.cbpi.ws.send(
                dict(
                    topic=self.update_key,
                    data=list(map(lambda item: item.to_dict(), self.data)),
                ),
                self.sorting,
            )
            self.cbpi.push_update("cbpi/actorupdate/{}".format(id), item.to_dict())

            # Report what the hardware is actually doing, not what was asked.
            # A caller that gets True from an emergency stop is entitled to
            # believe the element is de-energized.
            still_on = False
            try:
                still_on = item.instance.state is True
            except Exception:  # noqa: BLE001
                still_on = False
            if still_on:
                logging.error(
                    "Actor %s reports still ON after being switched off", id
                )
                return False
            return True
        except Exception as e:
            logging.error("Failed to switch off Actor {} {}".format(id, e))
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
        driving it from a sane one.

        An unreadable value fails to 0, not 100. This used to return 100, so
        `set_power(id, None)` or a malformed value from a plugin or an HTTP
        request went to FULL power on a multi-kilowatt element. When the number
        is meaningless the only defensible output is no heat.
        """
        try:
            return max(0, min(100, int(round(float(power)))))
        except (TypeError, ValueError):
            logging.error(
                "Uninterpretable power value %r - commanding 0%% instead", power
            )
            return 0

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
            if maxoutput is not None:
                item.maxoutput = maxoutput
            # Same clamp as every other path. This one reports the actor's level
            # to the interface and to any logic that reads it back, so an
            # unclamped or unreadable figure here becomes a bad control decision
            # somewhere else.
            item.power = self._clamp_power(power)
            if output is not None:
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
