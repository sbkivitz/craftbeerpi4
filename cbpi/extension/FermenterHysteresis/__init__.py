import asyncio
import logging
import time

from cbpi.api import *
from cbpi.api import readings
from cbpi.api.dataclasses import NotificationType
from cbpi.api.tasks import bounded
from cbpi.api.readings import finite
from cbpi.api.step import StepState
from cbpi.controller.fermentation_controller import FermentationController

class FermenterAutostart(CBPiExtension):
    """Bring every AutoStart fermenter back under temperature control on boot.

    AutoStart is a property of the fermenter LOGIC ("Autostart Fermenter on
    cbpi start"), but this started only the step profile: controller.start()
    resumes or begins a step and never starts the logic, which came on only if
    that step happened to say AutoMode Yes. So a fermenter with no steps - a
    glycol reservoir, a plain hold - had no temperature control after a reboot,
    and neither did one whose steps said AutoMode No, the setting that avoids
    AutoMode's end-of-step shutoff. And a profile nobody had started was
    started. Found by the fermenter twin: after a restart both logics were
    off and a freshly built profile was running (DEFECTS F2).

    Now, for each fermenter whose logic says AutoStart Yes:
      - the logic is started if it is not running;
      - a profile that was RUNNING when the server stopped is resumed. Not one
        the brewer paused: both are persisted as STOP, so the controller
        records which steps load() found ACTIVE (DEFECTS F4 - the first cut
        of this fix resumed paused profiles too). A profile nobody started is
        left alone.

    It runs as an on_startup initializer, after every controller has loaded.
    The constructor runs while plugins load, before sensors and actors exist,
    and the task it used to launch raced their initialisation.
    """

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.controller: FermentationController = cbpi.fermenter
        self.cbpi.register_on_startup(self)

    @on_startup("fermenter_autostart", order=100)
    async def run(self):
        logging.info("Starting Fermenter Autorun")
        for item in list(self.controller.data):
            try:
                if item.props is None or item.props.get("AutoStart", "No") != "Yes":
                    continue
                if item.instance is None or not item.instance.state:
                    await self.controller.start_logic(item.id)
                    logging.info("Autostart: temperature control on for %s", item.name)
                running = getattr(self.controller, "interrupted", {}).get(item.id)
                if running is not None:
                    # start() resumes the FIRST stopped step. Only let it if
                    # that is the one that was running - a paused step ahead of
                    # it would otherwise be resumed instead (DEFECTS F7).
                    first = self.controller._find_by_status(item.steps, StepState.STOP)
                    if first is not None and first.id == running:
                        await self.controller.start(item.id)
                        logging.info("Autostart: resumed the profile of %s", item.name)
                    else:
                        logging.warning(
                            "Autostart: %s was running step %s, but a paused step "
                            "comes before it - not resuming the profile. "
                            "Temperature control is on.", item.name, running)
            except Exception as e:  # noqa: BLE001 - one fermenter must not stop the rest
                logging.error("Autostart failed for fermenter %s: %s",
                              getattr(item, "name", "?"), e)


_finite = finite


class _GuardedHysteresis:
    """Hysteresis temperature control that survives what an unattended fermenter meets.

    Both bundled logics read the sensor with float(get_sensor_value(id)["value"])
    and the target with float() every second, and resolved the fermenter, its
    actors and its offsets once, at start. So:

      - a missing sensor, a probe that stopped updating, a "nan", a target of
        None: each raised, the task ended, and temperature control with it,
        while the switch went on showing it on (DEFECTS F8, F9);
      - a DS18B20 that browns out reads 85.0 C until it is next read, and this
        cooled against "85 C" flat out (F9);
      - an edited offset did nothing until a restart, and a cooler moved to
        another actor left the old one running (F10);
      - nothing stopped the heater and cooler running at once (F11).

    Now every tick re-reads the fermenter - sensor, actors, target, offsets -
    and acts only on a reading cbpi.api.readings trusts: finite, proven fresh
    by the sensor's own cadence, and a temperature a fermenter can have.
    Anything else switches heating and cooling off and keeps watching. The
    brewer is told once a fault has lasted FAULT_NOTIFY_AFTER ticks, reminded
    every FAULT_REMIND_EVERY seconds while it lasts, and told when control
    resumes, which it does by itself the moment the reading is usable again.

    Every command is checked against what the actor then reports. An actor
    that will not switch off is retried every tick and reported, and the
    other one is not started while it runs (F17).
    """

    # Consecutive bad ticks before anyone is told. At one tick a second this
    # rides out a single glitch without crying wolf.
    FAULT_NOTIFY_AFTER = 5

    # Wall-clock seconds between reminders while a fault lasts. A human
    # cadence, not a brewing one: an accelerated twin should not page anybody
    # faster than a real fermenter would.
    FAULT_REMIND_EVERY = 3600

    DEFAULT_MAX_AGE = readings.DEFAULT_MAX_AGE
    PLAUSIBLE_C = readings.FERMENTER_C

    # Used when an offset is left blank, as the original logic did.
    OFFSET_DEFAULTS = (
        ("HeaterOffsetOn", 0.2),
        ("HeaterOffsetOff", 0.0),
        ("CoolerOffsetOn", 0.2),
        ("CoolerOffsetOff", 0.0),
    )

    # Actors this logic drives, and switches off when it stops.
    ROLES = ("heater", "cooler")

    # Whether heater and cooler are driven at HeaterMaxPower / CoolerMaxPower.
    USES_MAX_POWER = False

    def _guard_reset(self):
        """Per-run state. Set here rather than in __init__, so a restarted run starts clean."""
        self._alerts = {}
        self._warned = set()
        self._deferred = set()
        self._deferred_now = set()
        self._tick_error = None
        self._valve_seen = None
        self._venting = False
        # actor id -> the role it last held here. Kept beyond this run, like
        # owned_actors: a heater reassigned away is still a heater until it
        # has been seen off (DEFECTS F32).
        self.__dict__.setdefault("_roles", {})
        self._missing_ticks = 0
        self.heater = self.cooler = None
        self.valve = None

    # --- reading the world ---------------------------------------------

    def _live_props(self, fermenter):
        # The fermenter's, not self.props: FermentationController.update()
        # replaces fermenter.props, and self.props is the object this logic
        # was started with (DEFECTS F10).
        props = getattr(fermenter, "props", None)
        return props if props is not None else (self.props or {})

    def _unit(self):
        return readings.temp_unit(self.cbpi)

    def _plausible(self):
        return readings.bounds(self._unit(), self.PLAUSIBLE_C)

    def _sensor_label(self, sensor_id):
        try:
            return self.get_sensor(sensor_id).name or sensor_id
        except Exception:  # noqa: BLE001
            return sensor_id

    def _reading(self, sensor_id, what, celsius):
        """(value, None) for a reading control may act on, else (None, (kind, why))."""
        if not sensor_id:
            return None, ("unassigned", "No {} sensor is assigned".format(what))
        try:
            state = self.get_sensor_value(sensor_id)
        except Exception:  # noqa: BLE001
            state = None
        return readings.assess(state, what, self._sensor_label(sensor_id), self._unit(),
                               celsius, self.DEFAULT_MAX_AGE)

    def _check_temperature(self, sensor_id):
        return self._reading(sensor_id, "temperature", self.PLAUSIBLE_C)

    def _check_target(self):
        try:
            raw = self.get_fermenter_target_temp(self.id)
        except Exception:  # noqa: BLE001
            raw = None
        target = _finite(raw)
        if target is None:
            return None, ("target", "Target temperature {!r} is not a number".format(raw))
        low, high = self._plausible()
        if not low <= target <= high:
            return None, ("target", "Target temperature {:.1f}{} is outside "
                                    "{:.0f} to {:.0f}".format(target, self._unit(), low, high))
        return target, None

    def _warn_once(self, key, message):
        if key in self._warned:
            return
        self._warned.add(key)
        logging.warning("Fermenter %s: %s", self._fermenter_name(), message)
        self._notify("{} configuration".format(self._fermenter_name()), message,
                     NotificationType.WARNING)

    def _number(self, props, name, default):
        """A numeric property; blank means the default, nonsense warns and uses it."""
        try:
            raw = props.get(name, None)
        except Exception:  # noqa: BLE001
            raw = None
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        value = _finite(raw)
        if value is None:
            self._warn_once(("prop", name, repr(raw)),
                            "{} is {!r}, not a number - using {}".format(name, raw, default))
            return default
        return value

    def _offsets(self, props, heater, cooler):
        h_on, h_off, c_on, c_off = (self._number(props, name, default)
                                    for name, default in self.OFFSET_DEFAULTS)
        if heater and h_on < h_off:
            self._warn_once(("inverted", "heater", h_on, h_off),
                            "HeaterOffsetOn ({:g}) is below HeaterOffsetOff ({:g}): the "
                            "heater will switch on and off every second near target".format(h_on, h_off))
        if cooler and c_on < c_off:
            self._warn_once(("inverted", "cooler", c_on, c_off),
                            "CoolerOffsetOn ({:g}) is below CoolerOffsetOff ({:g}): the "
                            "cooler will switch on and off every second near target".format(c_on, c_off))
        if heater and cooler and (h_off + c_on < 0 or c_off + h_on < 0):
            self._warn_once(("overlap", h_on, h_off, c_on, c_off),
                            "The heater and cooler offsets overlap, so one would start "
                            "while the other runs. They never run together: the one "
                            "running finishes first.")
        return h_on, h_off, c_on, c_off

    def _max_power(self, props, name):
        if not self.USES_MAX_POWER:
            return None
        value = self._number(props, name, 100)
        power = int(min(100, max(0, value)))
        if not 0 <= value <= 100:
            self._warn_once(("power", name, value),
                            "{} is {:g}% - using {}%".format(name, value, power))
        return power

    # --- actors ----------------------------------------------------------

    def _find_actor(self, actor_id):
        try:
            return self.cbpi.actor.find_by_id(actor_id)
        except Exception:  # noqa: BLE001
            return None

    def _actor(self, actor_id, role):
        """The assigned actor if it can be driven; None if unassigned or missing."""
        if not actor_id:
            self._clear_alert(("actor", role), None)
            return None
        actor = self._find_actor(actor_id)
        if actor is None or getattr(actor, "instance", None) is None:
            self._raise_alert(("actor", role),
                              ("missing", "The {} ({}) is missing or its driver is not "
                                          "running".format(role, actor_id)),
                              "it cannot be switched")
            return None
        self._clear_alert(("actor", role), "The {} can be switched again".format(role))
        return actor

    @staticmethod
    def _is_on(actor):
        try:
            return actor.instance.state is True
        except Exception:  # noqa: BLE001
            return False

    async def _switch(self, actor_id, on, power=None):
        """Command an actor; True once it reports the state asked for.

        Checked, not assumed. A switch-off that failed used to count as done,
        so the cooler was started beside a heater that was still running
        (DEFECTS F17).
        """
        if not on:
            # The base class's switch-off: bounded, and judged by the actor's
            # driver - a missing actor is not an off one (DEFECTS F31).
            done = await self._off_bounded(actor_id)
            if done:
                self._clear_alert(("stuck", actor_id), "{} switched off".format(actor_id))
                self._alerts.pop(("unresponsive", actor_id), None)
            else:
                self._raise_alert(("stuck", actor_id),
                                  ("stuck", "{} does not switch off".format(actor_id)),
                                  "retrying every second")
            return done
        # Owned before it is energized, so no failure from here on can leave
        # it running with nobody responsible for it (F19).
        self.owned_actors.add(actor_id)
        self._driver(actor_id)
        try:
            if self.USES_MAX_POWER:
                await bounded(self.actor_on(actor_id, power, power), self.ACTOR_TIMEOUT)
            else:
                await bounded(self.actor_on(actor_id), self.ACTOR_TIMEOUT)
        except asyncio.TimeoutError:
            logging.error("Fermenter %s: switching %s %s did not finish within %ss",
                          self._fermenter_name(), actor_id, "on" if on else "off",
                          self.ACTOR_TIMEOUT)
        except asyncio.CancelledError:
            if self._cancelled_by_request():
                raise
            logging.error("Fermenter %s: switching %s %s was cancelled",
                          self._fermenter_name(), actor_id, "on" if on else "off")
        except Exception as e:  # noqa: BLE001
            logging.error("Fermenter %s: switching %s %s failed: %r",
                          self._fermenter_name(), actor_id, "on" if on else "off", e)
        done = self._actor_on(actor_id) is True
        if done:
            self._clear_alert(("unresponsive", actor_id), "{} switches on again".format(actor_id))
        else:
            self._raise_alert(("unresponsive", actor_id),
                              ("unresponsive", "{} does not switch on".format(actor_id)),
                              "retrying every second")
        return done

    def _role_actors(self, role):
        """Every actor this logic owns that last held `role` here."""
        roles = self._roles
        assigned = {"heater": self.heater, "cooler": self.cooler}.get(role)
        found = {a for a in self.owned_actors if roles.get(a) == role}
        return found | ({assigned} if assigned else set())

    def _any_on(self, role):
        return any(self._actor_on(a) is True for a in self._role_actors(role))

    async def _retire(self, current):
        """Switch off every actor this logic owned that is no longer assigned to it.

        Kept until seen off: an actor whose switch-off failed was forgotten,
        and no later fault or stop ever tried it again (DEFECTS F19).
        """
        for actor_id in list(self.owned_actors - current):
            if await self._switch(actor_id, False):
                logging.info("Fermenter %s: %s is no longer assigned - switched off",
                             self._fermenter_name(), actor_id)
                self.owned_actors.discard(actor_id)

    async def _release_all(self):
        """Switch off everything this logic drives or drove."""
        await self._deenergize()

    # --- telling the brewer ------------------------------------------------

    def _alert_title(self, key):
        name = self._fermenter_name()
        if key == "control":
            return "{} temperature control".format(name)
        if key == "pressure":
            return "{} spunding".format(name)
        if isinstance(key, tuple) and key[0] == "actor":
            return "{} {}".format(name, key[1])
        return "{} actor".format(name)

    def _raise_alert(self, key, problem, consequence):
        # (kind, text), or (kind, text, seconds it may last before an alert).
        kind, text = problem[0], problem[1]
        notify_after = max(self.FAULT_NOTIFY_AFTER, problem[2] if len(problem) > 2 else 0)
        alert = self._alerts.get(key)
        if alert is None or alert["kind"] != kind:
            # INFO, not WARNING: with NOTIFY_ON_ERROR every warning is a
            # notification, and a one-tick glitch is not worth one.
            logging.info("Fermenter %s: %s - %s", self._fermenter_name(), text, consequence)
            if alert is None:
                alert = self._alerts[key] = {"ticks": 0, "notified_at": None}
            alert["kind"] = kind
        alert["ticks"] += 1
        alert["text"] = text
        now = time.monotonic()
        due = alert["notified_at"] is None or now - alert["notified_at"] >= self.FAULT_REMIND_EVERY
        if alert["ticks"] >= notify_after and due:
            alert["notified_at"] = now
            message = "{} - {}".format(text, consequence)
            logging.error("Fermenter %s: %s", self._fermenter_name(), message)
            self._notify(self._alert_title(key), message, NotificationType.ERROR)

    def _clear_alert(self, key, message):
        alert = self._alerts.pop(key, None)
        if alert is None:
            return
        if alert["notified_at"] is not None and message:
            logging.info("Fermenter %s: %s", self._fermenter_name(), message)
            self._notify(self._alert_title(key), message, NotificationType.INFO)
        else:
            logging.info("Fermenter %s: recovered after %d tick(s) (%s)",
                         self._fermenter_name(), alert["ticks"], alert.get("text"))

    # --- the loop ----------------------------------------------------------

    async def _suspend(self, problem):
        """No trustworthy reading or target: heating and cooling off, and say so."""
        entering = "control" not in self._alerts
        targets = {self.heater, self.cooler} | (self.owned_actors - {self.valve})
        for actor_id in [a for a in targets if a]:
            # Unconditionally on entry - the state flag may be the thing that
            # is wrong - and then whenever something has switched it back on.
            if entering or self._actor_on(actor_id) is not False:
                await self._switch(actor_id, False)
        self._raise_alert("control", problem, "heating and cooling are off until it is fixed")

    async def _fermenter_gone(self):
        """No fermenter by this id. Off at once; given up only once it has stayed gone.

        FermentationController.delete() stops the logic itself, so this is the
        fallback - and a lookup that misses once is not proof of a deletion.
        """
        for actor_id in [a for a in self.owned_actors | {self.heater, self.cooler} if a]:
            if self._missing_ticks == 0 or self._actor_on(actor_id) is not False:
                await self._switch(actor_id, False)
        self._missing_ticks += 1
        if self._missing_ticks < self.FAULT_NOTIFY_AFTER:
            return
        logging.warning("Fermenter %s no longer exists - its temperature control stops", self.id)
        self.stop_requested = True
        self.running = False
        self.state = False

    def _defer(self, role, why):
        if role not in self._deferred:
            logging.info("Fermenter %s: not starting the %s - %s",
                         self._fermenter_name(), role, why)
        self._deferred_now.add(role)

    async def _temperature_tick(self):
        self._deferred_now = set()
        fermenter = self.get_fermenter(self.id)
        if fermenter is None:
            await self._fermenter_gone()
            return
        self._missing_ticks = 0
        self.fermenter = fermenter
        props = self._live_props(fermenter)
        heater = getattr(fermenter, "heater", None) or None
        cooler = getattr(fermenter, "cooler", None) or None
        self.heater, self.cooler = heater, cooler
        current = self._assigned_actors(fermenter)
        self.owned_actors |= current
        for role in self.ROLES:
            actor_id = getattr(fermenter, role, None)
            if actor_id:
                self._roles[actor_id] = role
                self._driver(actor_id)
        await self._retire(current)

        if heater is not None and heater == cooler:
            await self._suspend(("config", "The same actor is set as both heater and cooler"))
            return
        reading, problem = self._check_temperature(getattr(fermenter, "sensor", None))
        if problem is None:
            target, problem = self._check_target()
        if problem is not None:
            await self._suspend(problem)
            return
        self._clear_alert("control", "Temperature reading is usable again - control resumed")

        h_on, h_off, c_on, c_off = self._offsets(props, heater, cooler)
        heater_actor = self._actor(heater, "heater")
        cooler_actor = self._actor(cooler, "cooler")
        heater_on = self._is_on(heater_actor)
        cooler_on = self._is_on(cooler_actor)
        if self._any_on("heater") and self._any_on("cooler"):
            # Heating and cooling at once - switched by hand, or left so by an
            # earlier configuration, or a former heater or cooler that has not
            # switched off. Never leave them fighting: all of them off, and
            # the rule decides afresh next tick.
            logging.info("Fermenter %s: heating and cooling were both on - switching all off",
                         self._fermenter_name())
            for actor_id in self._role_actors("heater") | self._role_actors("cooler"):
                await self._switch(actor_id, False)
            return

        # The rule itself is unchanged - the same comparisons, in the same
        # order, against the state read at the top of the tick. What is new is
        # that neither side is started while any actor of the other is
        # running - the assigned one or a former one still owned - judged
        # from what the actors report NOW, after any switch-off above
        # (DEFECTS F11, F17, F32).
        if heater_actor is not None:
            if reading + h_on <= target and not heater_on:
                if self._any_on("cooler"):
                    self._defer("heater", "a cooler is still running")
                else:
                    await self._switch(heater, True, self._max_power(props, "HeaterMaxPower"))
            if reading + h_off >= target and heater_on:
                await self._switch(heater, False)
        if cooler_actor is not None:
            if reading >= c_on + target and not cooler_on:
                if self._any_on("heater"):
                    self._defer("cooler", "a heater is still running")
                else:
                    await self._switch(cooler, True, self._max_power(props, "CoolerMaxPower"))
            if reading <= c_off + target and cooler_on:
                await self._switch(cooler, False)
        self._deferred = self._deferred_now
        self._tick_error = None

    async def _tick_failed(self, error):
        if self._tick_error != repr(error):
            self._tick_error = repr(error)
            logging.error("Fermenter %s: temperature control error",
                          self._fermenter_name(), exc_info=error)
        await self._suspend(("error", "Temperature control error: {!r}".format(error)))

    async def _temperature_loop(self):
        while self.running:
            try:
                await self._temperature_tick()
            except asyncio.CancelledError as e:
                if self._cancelled_by_request():
                    raise
                await self._tick_failed(e)
            except Exception as e:  # noqa: BLE001 - a bad tick must not end control
                await self._tick_failed(e)
            await asyncio.sleep(1)


@parameters(
    [
        Property.Number(
            label="HeaterOffsetOn",
            configurable=True,
            description="Offset as decimal number when the heater is switched on. Should be greater then 'HeaterOffsetOff'. For example a value of 2 switches on the heater if the current temperature is 2 degrees below the target temperature",
        ),
        Property.Number(
            label="HeaterOffsetOff",
            configurable=True,
            description="Offset as decimal number when the heater is switched off. Should be smaller then 'HeaterOffsetOn'. For example a value of 1 switches off the heater if the current temperature is 1 degree below the target temperature",
        ),
        Property.Number(
            label="CoolerOffsetOn",
            configurable=True,
            description="Offset as decimal number when the cooler is switched on. Should be greater then 'CoolerOffsetOff'. For example a value of 2 switches on the cooler if the current temperature is 2 degrees above the target temperature",
        ),
        Property.Number(
            label="CoolerOffsetOff",
            configurable=True,
            description="Offset as decimal number when the cooler is switched off. Should be smaller then 'CoolerOffsetOn'. For example a value of 1 switches off the cooler if the current temperature is 1 degree above the target temperature",
        ),
        Property.Number(
            label="HeaterMaxPower",
            configurable=True,
            description="Max Power [%] for Heater (default: 100)",
        ),
        Property.Number(
            label="CoolerMaxPower",
            configurable=True,
            description="Max Power [%] for Cooler (default: 100)",
        ),
        Property.Select(
            label="AutoStart",
            options=["Yes", "No"],
            description="Autostart Fermenter on cbpi start",
        ),
        Property.Sensor(
            label="sensor2", description="Optional Sensor for LCDisplay(e.g. iSpindle)"
        ),
    ]
)
class FermenterHysteresis(_GuardedHysteresis, CBPiFermenterLogic):

    USES_MAX_POWER = True
    ROLES = ("heater", "cooler")

    async def run(self):
        self._guard_reset()
        try:
            await self._temperature_loop()
        finally:
            self.running = False
            await self._release_all()


@parameters(
    [
        Property.Number(
            label="HeaterOffsetOn",
            configurable=True,
            description="Offset as decimal number when the heater is switched on. Should be greater then 'HeaterOffsetOff'. For example a value of 2 switches on the heater if the current temperature is 2 degrees below the target temperature",
        ),
        Property.Number(
            label="HeaterOffsetOff",
            configurable=True,
            description="Offset as decimal number when the heater is switched off. Should be smaller then 'HeaterOffsetOn'. For example a value of 1 switches off the heater if the current temperature is 1 degree below the target temperature",
        ),
        Property.Number(
            label="CoolerOffsetOn",
            configurable=True,
            description="Offset as decimal number when the cooler is switched on. Should be greater then 'CoolerOffsetOff'. For example a value of 2 switches on the cooler if the current temperature is 2 degrees above the target temperature",
        ),
        Property.Number(
            label="CoolerOffsetOff",
            configurable=True,
            description="Offset as decimal number when the cooler is switched off. Should be smaller then 'CoolerOffsetOn'. For example a value of 1 switches off the cooler if the current temperature is 1 degree above the target temperature",
        ),
        Property.Number(
            label="SpundingOffsetOpen",
            configurable=True,
            description="Offset above target pressure as decimal number when the valve is opened",
        ),
        Property.Select(
            label="ValveRelease",
            options=[1, 2, 3, 4, 5],
            description="Valve Release time in seconds",
        ),
        Property.Select(
            label="Pause",
            options=[1, 2, 3, 4, 5],
            description="Pause time in seconds between valve release",
        ),
        Property.Select(
            label="AutoStart",
            options=["Yes", "No"],
            description="Autostart Fermenter on cbpi start",
        ),
        Property.Sensor(
            label="sensor2", description="Optional Sensor for LCDisplay(e.g. iSpindle)"
        ),
    ]
)
class FermenterSpundingHysteresis(_GuardedHysteresis, CBPiFermenterLogic):
    """Temperature as FermenterHysteresis, plus a spunding valve held at target pressure.

    run() awaited the pressure loop first and the temperature loop second, so
    a temperature loop that died went unnoticed for as long as the pressure
    loop lived - the heater and cooler frozen in whatever state they were in
    (DEFECTS F12). Both loops are now watched together: either ending while
    the logic should be running fails the run, which the base class restarts.

    Venting is one valve pulse per look at the pressure. It used to be a loop
    of its own that held on to the sensor and valve it started with, so a
    sensor unassigned mid-vent went on being vented against (DEFECTS F24).
    """

    ROLES = ("heater", "cooler", "valve")

    # Shortest valve pulse or pause. A zero from a hand-edited profile made the
    # venting loop slam the solenoid as fast as the event loop would allow.
    MIN_VALVE_SECONDS = 1

    def _seconds(self, props, name, default):
        return max(self.MIN_VALVE_SECONDS, int(self._number(props, name, default)))

    def _check_pressure_target(self, fermenter):
        raw = getattr(fermenter, "target_pressure", None)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return 0.0, None  # no target: not spunding
        value = _finite(raw)
        if value is None:
            return None, ("target", "Target pressure {!r} is not a number".format(raw))
        return value, None

    async def _close_valve(self, valve=None):
        valve = valve or self.valve
        if valve and self._actor_on(valve) is not False:
            await self._switch(valve, False)

    async def _pressure_tick(self):
        """One look at the pressure, and at most one valve pulse. True if it pulsed."""
        fermenter = self.get_fermenter(self.id)
        if fermenter is None:
            self._venting = False
            return False  # the temperature loop stops the logic
        valve = getattr(fermenter, "valve", None) or None
        if valve != self._valve_seen:
            old, self._valve_seen = self._valve_seen, valve
            self._venting = False
            if old and await self._switch(old, False):
                self.owned_actors.discard(old)
            if valve:
                self.owned_actors.add(valve)
                await self._switch(valve, False)  # start closed, as it always did
        self.valve = valve
        sensor = getattr(fermenter, "pressure_sensor", None) or None
        if valve is None or sensor is None:
            self._venting = False
            await self._close_valve(valve)
            self._clear_alert("pressure", None)
            return False

        props = self._live_props(fermenter)
        offset = self._number(props, "SpundingOffsetOpen", 0)
        self.valverelease = self._seconds(props, "ValveRelease", 1)
        self.pause = self._seconds(props, "Pause", 2)
        target, problem = self._check_pressure_target(fermenter)
        if problem is None:
            pressure, problem = self._reading(sensor, "pressure", None)
        if problem is not None:
            self._venting = False
            await self._close_valve(valve)
            self._raise_alert("pressure", problem, "the spunding valve is kept closed")
            return False
        self._clear_alert("pressure", "Pressure reading is usable again - spunding resumed")
        if target == 0:
            self._venting = False
            await self._close_valve(valve)
            return False

        # Open at target + offset, keep pulsing down to target - as before.
        if not self._venting and pressure >= target + offset:
            self._venting = True
        elif self._venting and pressure < target:
            self._venting = False
        if not self._venting:
            return False
        await self._switch(valve, True)
        await asyncio.sleep(self.valverelease)
        await self._switch(valve, False)
        await asyncio.sleep(self.pause)
        return True

    async def pressure_control(self):
        while self.running:
            pulsed = False
            try:
                pulsed = await self._pressure_tick()
            except asyncio.CancelledError as e:
                if self._cancelled_by_request():
                    raise
                await self._pressure_failed(e)
            except Exception as e:  # noqa: BLE001 - a bad tick must not end spunding
                await self._pressure_failed(e)
            if not pulsed:
                await asyncio.sleep(1)

    async def _pressure_failed(self, error):
        logging.error("Fermenter %s: pressure control error",
                      self._fermenter_name(), exc_info=error)
        self._venting = False
        await self._close_valve()
        self._raise_alert("pressure", ("error", "Pressure control error: {!r}".format(error)),
                          "the spunding valve is kept closed")

    async def temperature_control(self):
        await self._temperature_loop()

    async def run(self):
        self._guard_reset()
        loops = [asyncio.create_task(self.temperature_control()),
                 asyncio.create_task(self.pressure_control())]
        self._own_tasks.update(loops)
        try:
            done, _ = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            if self.running:
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        raise task.exception()
                raise RuntimeError("a control loop ended while the logic was running")
        finally:
            self.running = False
            for task in loops:
                task.cancel()
            await asyncio.wait(loops)
            for task in loops:
                if not task.cancelled():
                    task.exception()  # retrieved, so asyncio does not warn about it
            self._own_tasks.difference_update(loops)
            await self._release_all()


def setup(cbpi):
    """
    This method is called by the server during startup
    Here you need to register your plugins at the server

    :param cbpi: the cbpi core
    :return:
    """
    cbpi.plugin.register("Fermenter Spunding Hysteresis", FermenterSpundingHysteresis)
    cbpi.plugin.register("Fermenter Hysteresis", FermenterHysteresis)
    cbpi.plugin.register("Fermenter Autostart", FermenterAutostart)
