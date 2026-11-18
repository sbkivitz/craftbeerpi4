"""The pre-brew check: what the brew-day screen asks before START begins a recipe (GET /step2/precheck).

Three things, in the brewer's order:

- probes: whether each kettle's probe gives a reading - a number, published, no older than its own limit, and a
  temperature a kettle can have - and if not, why, and for how long it hasn't;
- clock: whether the system clock is set, by the rule the steps and the brew-day timers use
  (cbpi.api.system_clock), with the reason in a form a screen can put in words;
- reminders: how many boil additions the loaded recipe has, and which have no reminder.

It says no more than it finds: a probe stuck on an old value that it keeps publishing still gives a reading. It
starts, stops and switches nothing. What to say, and whether to start anyway, is the screen's.

Kettles only: fermenters are left out.
"""

import asyncio
import logging
import time

from cbpi.api import clock, readings, system_clock
from cbpi.controller.upload_controller import UploadController
from cbpi.extension.hltpump import kettle_roles

# Real time, apart from the brewing clock: how old the answer about the system clock is.
_REAL = clock.RealTimeSource()

#: Why a probe gives no reading, from readings.assess's kind of problem.
_REASONS = {"silent": "unreadable", "unproven": "never", "stale": "stale", "value": "not_a_number",
            "implausible": "out_of_range"}


def _seconds(value):
    number = readings.finite(value)
    return None if number is None else round(number, 1)


class PrecheckController:
    #: Real seconds an answer about the system clock is kept. A check made with an older one asks again first, so a
    #: screen asking every 5 s sees the clock become set within about 10 s, and timedatectl runs at most about once
    #: in this long however many screens ask.
    CLOCK_FRESH = 5.0
    #: Real seconds a check waits for that new answer before it answers with the one it has: well inside the 5 s a
    #: screen waits for the check, though timedatectl can take TIMEDATECTL_TIMEOUT to fail.
    CLOCK_WAIT = 2.0
    #: The order the screen names the vessels in, by their roles (cbpi.extension.hltpump.kettle_roles): the HLT, the
    #: mash tun, the boil kettle. Any other kettle follows, in CraftBeerPi's order.
    ROLES = ("hlt", "mt", "bk")

    def __init__(self, cbpi):
        self.cbpi = cbpi
        self.logger = logging.getLogger(__name__)
        # When the check was made, at start-up, on the clock sensor ages are measured on: how long a probe that has
        # never given a reading has been silent, if the probe can't say when it was started.
        self._started = time.monotonic()
        # The latest answer about the system clock (system_clock.clock_answer), and _REAL.monotonic() when it was
        # asked for; the task asking now, if one is.
        self._answer = None
        self._asked_at = None
        self._asking = None
        app = getattr(cbpi, "app", None)
        if app is not None:
            app.on_cleanup.append(self.shutdown)

    async def shutdown(self, app=None):
        if self._asking is not None and not self._asking.done():
            self._asking.cancel()

    async def check(self):
        """The whole check: dict(ok, probes, clock, reminders), each part with its own ok. A part that fails to
        work itself out says so in "error", with ok false, and the other two still answer."""
        clock_part = await self._part("clock", self.clock)
        probes = await self._part("probes", self.probes)
        reminders = await self._part("reminders", self.reminders)
        return dict(ok=probes["ok"] and clock_part["ok"] and reminders["ok"], probes=probes, clock=clock_part,
                    reminders=reminders)

    async def _part(self, name, work):
        try:
            result = work()
            return await result if asyncio.iscoroutine(result) else result
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one part that can't be worked out must not hide the others
            self.logger.exception("Pre-brew check: could not check the %s", name)
            return dict(ok=False, error=str(e) or type(e).__name__)

    # -- probes ---------------------------------------------------------------------------------------------------

    def probes(self):
        """dict(ok, unit, range, kettles): whether every kettle's probe gives a reading.

        unit is the TEMP_UNIT setting's, "F" or "C"; range the temperatures a kettle can have in it (KETTLE_C):
        [-4.0, 230.0] in F. kettles has one entry for each kettle - the HLT, the mash tun and the boil kettle first,
        as the screen names them - see _probe."""
        unit = readings.temp_unit(self.cbpi)
        low, high = readings.bounds(unit, readings.KETTLE_C)
        kettles = [self._probe(kettle, role, unit) for kettle, role in self._kettles()]
        return dict(ok=all(k["ok"] for k in kettles), unit=unit, range=[low, high], kettles=kettles)

    def _kettles(self):
        """[(kettle, role), ...] in ROLES' order, then any other kettle. role None for one the screen has no role for."""
        kettles = [k for k in (getattr(self.cbpi.kettle, "data", None) or []) if k is not None]
        try:
            roles = kettle_roles(self.cbpi)
        except Exception as e:  # noqa: BLE001 - every kettle is still checked, in CraftBeerPi's order
            self.logger.warning("Pre-brew check: could not tell which kettle is which: %s", e)
            roles = {}
        role_of = {}
        for role in self.ROLES:
            kettle = roles.get(role)
            if kettle is not None and kettle.id not in role_of:
                role_of[kettle.id] = role
        rank = {role: i for i, role in enumerate(self.ROLES)}
        kettles.sort(key=lambda k: rank.get(role_of.get(k.id), len(self.ROLES)))
        return [(k, role_of.get(k.id)) for k in kettles]

    def _probe(self, kettle, role, unit):
        """Whether one kettle's probe gives a reading:

        dict(kettle_id, kettle, role, sensor_id, probe, ok, reason, value, age, max_age, no_reading_for). kettle
        and probe are their names in CraftBeerPi ("Boil Kettle", "Boil Temp"). value, age and max_age are as
        SensorController.get_sensor_value gives them (value null when it isn't a number). reason, when ok is false:
        - "no_probe": the kettle has no probe set. It counts as no reading: a kettle a recipe heats needs one;
        - "missing": its probe isn't one of CraftBeerPi's sensors any more;
        - "unreadable": its sensor isn't running, or doesn't report a reading as CraftBeerPi's sensors do;
        - "never": no reading since CraftBeerPi started it;
        - "stale": its last reading is older than its own limit, max_age;
        - "not_a_number": its latest reading isn't a number;
        - "out_of_range": its latest reading is no temperature a kettle can have.
        no_reading_for: how long it hasn't given a reading, in seconds on the monotonic real clock sensor ages are
        measured on (SensorController.sample_age, time.monotonic), whatever the brewing clock's speed. For "never",
        since CraftBeerPi started the sensor; for "stale", the age of its last reading; for "not_a_number" and
        "out_of_range", that age too once it is past max_age - otherwise, and for the first three, null: not known.
        """
        sensor_id = getattr(kettle, "sensor", None) or None
        item = dict(kettle_id=kettle.id, kettle=kettle.name, role=role, sensor_id=sensor_id, probe=None, ok=False,
                    reason=None, value=None, age=None, max_age=None, no_reading_for=None)
        if sensor_id is None:
            item["reason"] = "no_probe"
            return item
        sensor = self.cbpi.sensor.find_by_id(sensor_id)
        if sensor is None:
            item["reason"] = "missing"
            return item
        item["probe"] = sensor.name
        instance = getattr(sensor, "instance", None)
        state = self.cbpi.sensor.get_sensor_value(sensor_id) if instance is not None else None
        _, problem = readings.assess(state, unit=unit, celsius=readings.KETTLE_C)
        if isinstance(state, dict):
            item["value"] = readings.finite(state.get("value"))
            item["age"] = _seconds(state.get("age"))
            item["max_age"] = _seconds(state.get("max_age"))
        if problem is None:
            item["ok"] = True
            return item
        item["reason"] = _REASONS.get(problem[0], problem[0])
        item["no_reading_for"] = self._silent_for(item, instance)
        return item

    def _silent_for(self, item, instance):
        reason, age, limit = item["reason"], item["age"], item["max_age"]
        if reason == "never":
            started = getattr(instance, "started_monotonic", None)
            if not isinstance(started, (int, float)):
                started = self._started
            return round(max(0.0, time.monotonic() - started), 1)
        if age is None or age < 0:
            return None
        if reason == "stale" or (reason in ("not_a_number", "out_of_range") and limit is not None and age > limit):
            return age
        return None

    # -- the system clock ----------------------------------------------------------------------------------------

    async def clock(self):
        """dict(ok, source, reason, detail, age): whether the system clock counts as set.

        source, reason and detail are system_clock.clock_answer's: where the time comes from under today's rule,
        and why it does or doesn't count as set, for the screen to put in words. reason "no_answer_yet": timedatectl
        hasn't answered the first time it was asked, within CLOCK_WAIT; ok is false then. age: real seconds since
        timedatectl was asked for this answer.

        The answer is asked for in a thread, as it runs a program, and kept for CLOCK_FRESH seconds. A check with
        an older one waits up to CLOCK_WAIT for the new answer, and the event loop runs on meanwhile."""
        if self._answer is None or _REAL.monotonic() - self._asked_at >= self.CLOCK_FRESH:
            try:
                await asyncio.wait_for(asyncio.shield(self._ask()), self.CLOCK_WAIT)
            except asyncio.TimeoutError:
                pass
        answer = self._answer
        if answer is None:
            return dict(ok=False, source=None, reason="no_answer_yet", detail=None, age=None)
        return dict(ok=answer.get("trusted") is True, source=answer.get("source"), reason=answer.get("reason"),
                    detail=answer.get("detail"), age=round(max(0.0, _REAL.monotonic() - self._asked_at), 1))

    def _ask(self):
        """The task asking about the system clock: the one already asking, or a new one. One at a time."""
        if self._asking is None or self._asking.done():
            self._asking = asyncio.get_running_loop().create_task(self._ask_now())
        return self._asking

    async def _ask_now(self):
        asked = _REAL.monotonic()
        try:
            answer = await asyncio.get_running_loop().run_in_executor(None, system_clock.clock_answer)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - not asked counts as not confirmed, as the rule says
            self.logger.warning("Pre-brew check: could not ask whether the system clock is set: %s", e)
            answer = dict(trusted=False, source=None, reason="unconfirmed",
                          detail="couldn't ask whether the clock is set ({})".format(e))
        self._answer, self._asked_at = answer, asked

    # -- reminders -------------------------------------------------------------------------------------------------

    def reminders(self):
        """dict(ok, additions, without_reminder, boil_steps, per_boil_step): whether every boil addition of the
        loaded recipe has a reminder.

        additions counts the reminders set in every boil step of the profile - Hop_1 to Hop_6, each holding minutes
        BoilStep can remind at - and the additions its import couldn't give one (StepController.
        additions_without_reminder). without_reminder lists those, [{"name", "minutes"}, ...], longest first: the
        name as the import's warning named it ("1.50 oz Citra"), minutes before the end of the boil. A recipe built
        by hand has none. A boil step is a BoilStep, or a step type derived from it. per_boil_step is how many
        reminders one holds."""
        steps = getattr(self.cbpi.step, "profile", None) or []
        types = getattr(self.cbpi.step, "types", None) or {}
        holds = UploadController.BOIL_REMINDERS
        boil = [s for s in steps if self._is_boil(s, types)]
        reminded = 0
        for step in boil:
            props = step.props
            for n in range(1, holds + 1):
                if UploadController._reminder_minutes(props.get("Hop_%d" % n) if props is not None else None) \
                        is not None:
                    reminded += 1
        missing = self.cbpi.step.additions_without_reminder()
        return dict(ok=not missing, additions=reminded + len(missing), without_reminder=missing,
                    boil_steps=len(boil), per_boil_step=holds)

    @staticmethod
    def _is_boil(step, types):
        if step.type == "BoilStep":
            return True
        boil = (types.get("BoilStep") or {}).get("class")
        own = (types.get(step.type) or {}).get("class")
        return isinstance(boil, type) and isinstance(own, type) and issubclass(own, boil)
