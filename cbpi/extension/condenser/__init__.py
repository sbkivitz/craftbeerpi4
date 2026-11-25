"""Turn the steam condenser's mister off once the boil is over and the wort has cooled.

A boil kettle with a steam condenser fitted on its lid (cbpi.api.condenser, cbpi.api.kettle_measures) keeps its mister
on after the boil, until the wort falls back below the lid reminder's own temperature (lid_temp: 95 in C, 203 in F).
The watch here does it:

- It starts when the boil step ends as done - its time up, or NEXT, on a running or a stopped boil - never when it is
  stopped: BoilStep.on_stop hands it over (after_boil), and the step listener catches a stopped boil skipped with
  NEXT. Only if the mister was asked on this brew: the lid reminder answered, for a mister set by hand; the boil
  switched steps_boil_actor on, for one CraftBeerPi switches. Once a brew: not again once it has acted.
- It reads the boil kettle's probe each second on CraftBeerPi's clock (cbpi.api.clock), so it acts at the same brewing
  moment at any SIM_TIME_SCALE. The first reading below lid_temp ends it, including one there as the boil ends. A
  reading that isn't a number, or none, never counts. A dip during the boil changes nothing: the watch isn't running.
- Set by hand: one reminder, kept on every page until answered, "Turn off the {name} mister" / "The boil is over and
  the wort is below 203 °F", with Done (kind "condenser_off").
- Switched: steps_boil_actor off, with a log notification "{Name} mister off: the boil is over and the wort is below
  203 °F", and no question, as there is nothing to do. BoilStep.on_stop leaves it on as the boil ends for this.
- It outlives the boil step and the recipe's end. A recipe loaded, reset or unloaded ends it without asking, taking a
  waiting reminder down. Switched, those and STOP switch the actor off at once, and so does the watch ending any
  other way, the server's end among them. It never switches anything on: an actor found off (ALL OFF, or by hand)
  ends it, with nothing left to do.
- What it has done is kept with the boil step's props (cbpi.api.condenser), so after a restart it carries on watching,
  or gives a waiting reminder again, and never acts twice.

Without a condenser fitted on the boil step's kettle nothing here runs, and steps_boil_actor goes off as the boil ends.
"""

import asyncio
import logging
import math

from cbpi.api import CBPiExtension, clock, kettle_measures
from cbpi.api import condenser as cc
from cbpi.api.dataclasses import NotificationAction, NotificationType
from cbpi.api.step import StepState

logger = logging.getLogger(__name__)

HAND = "hand"
SWITCHED = "switched"


def _setting(cbpi, key, default=None):
    try:
        return cbpi.config.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _on(actor):
    return getattr(getattr(actor, "instance", None), "state", False) is True


def _number(value):
    """A reading as a number, or None for one that isn't: None, NaN, infinite, text."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class Watch:
    """One boil's watch, from the boil's end to the wort below lid_temp."""

    def __init__(self, owner, step_id, mode, sensor_id, actor_id, name, unit):
        self.owner, self.cbpi = owner, owner.cbpi
        self.step_id, self.mode, self.sensor_id, self.actor_id = step_id, mode, sensor_id, actor_id
        self.name, self.unit = name, unit
        self.limit = cc.lid_temp(unit)
        # Done: it has acted, or there is nothing left for it to do; the actor is then the brewer's.
        self.done = False
        self.task = None

    def start(self):
        self.task = asyncio.ensure_future(self.run())
        return self

    def actor(self):
        actors = getattr(self.cbpi, "actor", None)
        return actors.find_by_id(self.actor_id) if actors is not None and self.actor_id else None

    def holding(self):
        """Is this watch keeping steps_boil_actor on for now?"""
        return self.mode == SWITCHED and not self.done and _on(self.actor())

    def gone(self):
        """The boil's step is no longer this brew's: the recipe was reset, loaded or unloaded."""
        item = self.cbpi.step.find_by_id(self.step_id)
        return item is None or item.status == StepState.INITIAL

    def reading(self):
        try:
            got = self.cbpi.sensor.get_sensor_value(self.sensor_id)
        except Exception as e:  # noqa: BLE001 - no reading is no reason to act
            logger.debug("Steam condenser: could not read the boil kettle: %s", e)
            return None
        return _number(got.get("value") if isinstance(got, dict) else got)

    async def run(self):
        try:
            while not self.done:
                if self.gone():
                    return
                if self.mode == SWITCHED and not _on(self.actor()):
                    # Off already - ALL OFF, or by hand: nothing left to do, and never switched on again.
                    self.done = True
                    await self.owner.record(self.step_id, cc.OFF_ASKED_PROP, cc.OFF_ANSWERED_PROP)
                    return
                value = self.reading()
                if value is not None and value < self.limit:
                    await self.act()
                    return
                await clock.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("Steam condenser: the watch after the boil failed: %s", e, exc_info=True)
        finally:
            # A switched mister is never left on by a watch that ends without acting.
            if self.mode == SWITCHED and not self.done:
                await self.owner.switch_off(self.actor_id)

    async def act(self):
        """The wort is below lid_temp after the boil."""
        self.done = True
        words = cc.degrees(self.unit)
        if self.mode == SWITCHED:
            was_on = _on(self.actor())
            await self.owner.record(self.step_id, cc.OFF_ASKED_PROP, cc.OFF_ANSWERED_PROP)
            if was_on:
                await self.owner.switch_off(self.actor_id)
                logger.info("Steam condenser: %s mister off, the wort below %s after the boil", self.name, words)
                self.cbpi.notify(cc.SWITCHED_OFF_TITLE.format(cc.capitalised(self.name)),
                                 cc.SWITCHED_OFF_TEXT.format(words), NotificationType.INFO)
            return
        await self.owner.record(self.step_id, cc.OFF_ASKED_PROP)
        self.owner.ask(self.step_id, self.name, self.unit)


class SteamCondenser(CBPiExtension):

    def __init__(self, cbpi):
        self.cbpi = cbpi
        # This brew's watch, from the boil's end until a reset, a load or an unload (end).
        self.watch = None
        # The turn-off reminder waiting for its Done: (step id, notification id), or None.
        self.waiting = None
        # Switch-offs and saves while they run: for a test to wait on.
        self._later_tasks = set()
        cbpi.condenser = self
        add = getattr(getattr(cbpi, "step", None), "add_listener", None)
        if callable(add):
            add(self.step_event)
        app = getattr(cbpi, "app", None)
        if app is not None:
            app.on_startup.append(self._resume)
            app.on_cleanup.append(self.shutdown)

    # ---------------------------------------------------------------- when it starts
    def due(self, item):
        """The watch a done boil `item` (a step) needs, as (mode, sensor id, actor id, name, unit), or None: none
        fitted, or the mister wasn't asked on this brew, or the watch has acted."""
        if item is None or getattr(item, "type", None) != cc.BOIL_TYPE:
            return None
        props = item.props
        if props.get(cc.OFF_ASKED_PROP, False):
            return None
        kettles = getattr(self.cbpi, "kettle", None)
        kettle = kettles.find_by_id(props.get("Kettle", None)) if kettles is not None else None
        if not kettle_measures.condenser_fitted(kettle):
            return None
        actor_id = _setting(self.cbpi, "steps_boil_actor", None) or None
        actors = getattr(self.cbpi, "actor", None)
        switched = actor_id is not None and actors is not None and actors.find_by_id(actor_id) is not None
        if switched and not props.get(cc.ACTOR_ON_PROP, False):
            return None
        if not switched and not props.get(cc.LID_ANSWERED_PROP, False):
            return None
        sensor_id = props.get("Sensor", None) or getattr(kettle, "sensor", None)
        return (SWITCHED if switched else HAND, sensor_id, actor_id if switched else None,
                kettle_measures.condenser_name(kettle), _setting(self.cbpi, "TEMP_UNIT", "C"))

    def begin(self, step_id):
        """Start watching after the boil step `step_id`, if it needs it. Once a brew: a boil already watched keeps
        its watch, acted or not, until end(). -> the watch, or None."""
        if self.watch is not None and self.watch.step_id == step_id:
            return self.watch
        need = self.due(self.cbpi.step.find_by_id(step_id))
        if need is None:
            return None
        self.end()
        mode, sensor_id, actor_id, name, unit = need
        logger.info("Steam condenser: the boil is over; the %s mister (%s) goes off below %s", name,
                    "switched" if mode == SWITCHED else "set by hand", cc.degrees(unit))
        self.watch = Watch(self, step_id, mode, sensor_id, actor_id, name, unit).start()
        return self.watch

    async def after_boil(self, step):
        """BoilStep.on_stop, the boil ended as done (its time up, or NEXT). -> True if the watch now keeps
        steps_boil_actor on, for the step to leave on."""
        try:
            watch = self.begin(step.id)
        except Exception as e:  # noqa: BLE001 - the boil ends whatever this does
            logger.error("Steam condenser: could not start the watch after the boil: %s", e, exc_info=True)
            return False
        return watch is not None and watch.holding()

    # ---------------------------------------------------------------- the brew steps
    def step_event(self, event, snapshot):
        """A step listener (StepController.add_listener)."""
        if event == "step_done":
            step_id = (snapshot or {}).get("step_id")
            item = self.cbpi.step.find_by_id(step_id) if step_id else None
            if item is not None and item.status == StepState.DONE:
                self.begin(step_id)
        elif event in ("reset", "loaded"):
            # Another brew, or this one from the start: nothing more to watch or ask.
            self.end(retire=True)
        elif event == "stopped":
            # STOP: a switched mister goes off at once, as the boil's own STOP has always switched it off.
            watch = self.watch
            if watch is not None and watch.mode == SWITCHED and not watch.done:
                watch.done = True
                if watch.task is not None:
                    watch.task.cancel()
                self._later(self.switch_off(watch.actor_id))
                self._later(self.record(watch.step_id, cc.OFF_ASKED_PROP, cc.OFF_ANSWERED_PROP))

    def end(self, retire=False):
        """End this brew's watch without asking; a switched mister goes off. With `retire`, a waiting turn-off
        reminder is taken down too, no longer needed."""
        watch, self.watch = self.watch, None
        if watch is not None and not watch.done:
            watch.done = True
            if watch.task is not None:
                watch.task.cancel()
            if watch.mode == SWITCHED:
                self._later(self.switch_off(watch.actor_id))
        if retire and self.waiting is not None:
            _, notification_id = self.waiting
            self.waiting = None
            retire_fn = getattr(getattr(self.cbpi, "notification", None), "retire", None)
            if callable(retire_fn):
                try:
                    retire_fn(notification_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("Steam condenser: could not take the reminder down: %s", e)

    # ---------------------------------------------------------------- what it does
    def ask(self, step_id, name, unit):
        """The reminder to turn a mister set by hand off, kept on every page until its Done."""
        asked = {}

        async def answered():
            if self.waiting is not None and self.waiting[1] == asked.get("id"):
                self.waiting = None
            item = self.cbpi.step.find_by_id(step_id)
            if item is not None and item.status == StepState.DONE and item.props.get(cc.OFF_ASKED_PROP, False):
                await self.record(step_id, cc.OFF_ANSWERED_PROP)

        logger.info("Steam condenser: asking for the %s mister to be turned off", name)
        asked["id"] = self.cbpi.notify(cc.OFF_TITLE.format(name), cc.OFF_TEXT.format(cc.degrees(unit)),
                                       NotificationType.WARNING, action=[NotificationAction("Done", answered)],
                                       keep=True, kind=cc.KIND_OFF)
        if asked["id"] is not None:
            self.waiting = (step_id, asked["id"])
        return asked["id"]

    async def record(self, step_id, *keys):
        """Set each of `keys` on the boil step's props, and save the brew steps. Never raises."""
        item = self.cbpi.step.find_by_id(step_id)
        if item is None:
            return
        for key in keys:
            item.props[key] = True
        try:
            await self.cbpi.step.save()
        except Exception as e:  # noqa: BLE001
            logger.warning("Steam condenser: could not save what the watch did: %s", e)

    async def switch_off(self, actor_id):
        """Switch steps_boil_actor off if it is on. Never raises."""
        actors = getattr(self.cbpi, "actor", None)
        actor = actors.find_by_id(actor_id) if actors is not None and actor_id else None
        if actor is None or not _on(actor):
            return
        try:
            await actors.off(actor_id)
        except Exception as e:  # noqa: BLE001 - say so: a mister left on wastes water
            logger.error("Steam condenser: could not switch the mister (%s) off: %s", actor_id, e)

    def _later(self, coro):
        task = asyncio.ensure_future(coro)
        self._later_tasks.add(task)
        task.add_done_callback(self._later_tasks.discard)
        return task

    # ---------------------------------------------------------------- a restart
    async def _resume(self, app=None):
        """After a restart: watch again after a done boil whose watch hadn't acted, or give its waiting reminder
        again."""
        try:
            for item in list(getattr(self.cbpi.step, "profile", None) or []):
                if getattr(item, "type", None) != cc.BOIL_TYPE or item.status != StepState.DONE:
                    continue
                props = item.props
                if not props.get(cc.OFF_ASKED_PROP, False):
                    self.begin(item.id)
                elif not props.get(cc.OFF_ANSWERED_PROP, False) and self.waiting is None:
                    kettle = self.cbpi.kettle.find_by_id(props.get("Kettle", None))
                    if kettle_measures.condenser_fitted(kettle):
                        self.ask(item.id, kettle_measures.condenser_name(kettle),
                                 _setting(self.cbpi, "TEMP_UNIT", "C"))
        except Exception as e:  # noqa: BLE001 - starting up goes on whatever this does
            logger.warning("Steam condenser: could not pick the watch up after a restart: %s", e)

    async def shutdown(self, app=None):
        watch = self.watch
        if watch is not None and watch.task is not None and not watch.task.done():
            watch.task.cancel()
            try:
                await watch.task
            except BaseException:  # noqa: BLE001
                pass


def setup(cbpi):
    cbpi.plugin.register("SteamCondenser", SteamCondenser)
