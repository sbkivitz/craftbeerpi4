"""Run the HLT pump whenever the HLT heats, until the sparge.

Water heated from below settles into layers, hot on top, so the HLT's probe
and the HERMS coil each sit in different water. The HLT pump keeps the HLT
stirred to prevent that; on the rig it is Recirc_PUMP, the HLT kettle's
agitator. From the sparge on it has another job: for a fly sparge it moves
the sparge water to the top of the mash tun.

The HLT's own logic (PIDBoil) switches only the element, the mash tun's
PID_HERMS only its own pump - the wort pump - and an element switched on by
hand is just the element: none of them runs the HLT pump.

So, on the server, with no page open too:
- Before the sparge, when the HLT starts heating - its own AUTO, the mash
  tun's HERMS, or its element switched on - the HLT pump is switched on, and
  a notification says why and asks for its valves to be checked.
- Including when nobody is there: BrewStart heating the water before the
  brewer is up. A BrewStart step starting asks for the pump and its valves to
  be checked, and waits for an answer. Started with its valves set to move
  water to the mash tun, the pump would empty the HLT into it and could leave
  the element dry.
- It is left on when the heating stops, so it runs the whole time before the
  sparge rather than stopping and starting between rests.
- The brewer can still stop it, and ALL OFF does. It starts again the next
  time the HLT starts heating, which includes something else taking the
  heating over - the recipe's water heating handing over to the mash tun's
  HERMS. A stop is never fought: the element pulsing under a running logic is
  not a start.
- From the sparge on - once the recipe reaches the first step named for the
  runoff, as the first-wort reminder finds it - it is the brewer's, to set the
  valves for the sparge water and start. A recipe that is over, every step
  done, counts as none.
- HLT_PUMP_WITH_HEAT = No turns all of this off.

And whatever the setting, the HLT pump is switched off as the runoff ends.
Running on after the Sparge step's time is up, it would send sparge water to
the mash tun with nothing taking wort out; and a pump is off whenever the
brewer has hoses or valves to change. So the HLT pump is switched off both
when the sparge water is ready (SpargeStep) and when the sparge ends: when the
last step named for the runoff ends - its time up, or NEXT - with the HLT pump
running, it is switched off and a notification says so. The wort pump is left
as it is, to finish draining the mash tun; starting the boil switches it off
(BoilStep). A
runoff step followed by another - Sparge, then Lautering - is one runoff. A
Stop is a pause, and leaves both pumps as they are.

The steps switch pumps off for the brewer's hoses through pump_off and
runoff_pumps_off below.

The HLT is found as the brew-day screen finds it (brewday/live.js
kettleRoles): the kettle the mash tun's HERMS works on - its HLT_Kettle, else
the kettle whose sensor is its HLT_Sensor - else the one sharing the mash
tun's heater, else the one called HLT. Its heater is the HLT element and its
agitator the HLT pump. A pump that is also another kettle's agitator is left
alone: a HERMS runs it by its own rules (MashIn_Pump_Within).
"""

import asyncio
import logging
import re
import types

from cbpi.api import CBPiExtension, clock
from cbpi.api.dataclasses import NotificationAction, NotificationType
from cbpi.api.step import StepState

logger = logging.getLogger(__name__)

SETTING = "HLT_PUMP_WITH_HEAT"

#: The steps that start the brew later by themselves, with nobody there:
#: cbpi4_BrewStart's step.
SCHEDULED_STEPS = ("BrewStart",)

CHECK_TITLE = "Check the HLT pump"
CHECK_TEXT = ("The HLT pump will start when the heating does, and nobody may be there. "
              "Before you leave it: make sure the pump is ready to go and its valves are "
              "open to recirculate the HLT. ({} in Settings)")
STARTED_TEXT = ("Switched on with the HLT heat, to keep the water from settling into hot "
                "and cool layers. Check its valves are set to recirculate the HLT. ({} in Settings)")
RUNOFF_TITLE = "HLT pump"
RUNOFF_OFF_TEXT = "{} has ended, so the HLT pump is off."
RUNOFF_WORT_TEXT = "The wort pump is still running, to finish draining the mash tun."
RUNOFF_FAILED_TITLE = "HLT pump still running"
RUNOFF_FAILED_TEXT = ("{} has ended, but the HLT pump could not be switched off. If it is still sending "
                      "sparge water to the mash tun, the mash tun will fill up: stop it by hand.")


def _setting(cbpi, key, default=None):
    try:
        return cbpi.config.get(key, default)
    except Exception:  # noqa: BLE001
        return default


def _prop(item, key):
    props = getattr(item, "props", None)
    try:
        return props.get(key) if props is not None else None
    except Exception:  # noqa: BLE001
        return None


def _herms_logics(cbpi):
    """The kettle logics that run a HERMS: those with an HLT_Sensor setting, as PID_HERMS has."""
    found = set()
    for name, t in (getattr(cbpi.kettle, "types", None) or {}).items():
        props = (t or {}).get("properties") or []
        if any(isinstance(p, dict) and p.get("label") == "HLT_Sensor" for p in props):
            found.add(name)
    return found


def find_hlt(cbpi):
    """The HLT kettle, or None. See the module's notes."""
    return kettle_roles(cbpi)["hlt"]


def kettle_roles(cbpi):
    """{"hlt", "mt", "bk"}: the HLT, mash tun and boil kettle, each a kettle or None,
    found as the brew-day screen finds them (brewday/live.js kettleRoles)."""
    kettles = [k for k in (getattr(cbpi.kettle, "data", None) or []) if k is not None]
    by_id = {k.id: k for k in kettles}
    herms = _herms_logics(cbpi)

    def is_herms(k):
        return k.type in herms if herms else bool(re.search("herms", str(k.type or ""), re.I))

    def named(pattern, among):
        return next((k for k in among if re.search(pattern, str(k.name or ""), re.I)), None)

    mt = (by_id.get(_setting(cbpi, "MASH_TUN")) or next((k for k in kettles if is_herms(k)), None)
          or named("mash", kettles))
    others = [k for k in kettles if k is not mt]
    bk = (by_id.get(_setting(cbpi, "BoilKettle")) or named("boil", others)
          or next((k for k in others if re.search("boil", str(k.type or ""), re.I)), None))
    rest = [k for k in kettles if k is not mt and k is not bk]
    hk, hs = _prop(mt, "HLT_Kettle"), _prop(mt, "HLT_Sensor")
    hlt = ((hk and next((k for k in rest if k.id == hk), None))
           or (hs and next((k for k in rest if k.sensor == hs), None))
           or (mt is not None and mt.heater and next((k for k in rest if k.heater == mt.heater), None))
           or named(r"\bhlt\b|hot liquor", rest)
           or (rest[0] if len(rest) == 1 else None))
    return {"hlt": hlt or None, "mt": mt if mt is not None and mt is not bk else None, "bk": bk}


def _on(item):
    return getattr(getattr(item, "instance", None), "state", False) is True


def heat_source(cbpi, hlt):
    """What is heating the HLT: "own" (its own logic), "herms" (another
    kettle's logic on its element - the mash tun's HERMS), "hand" (its element
    switched on by hand), or None.

    A logic counts the whole time it runs, not each time it pulses the element,
    so a pump the brewer stops is not started again at the next pulse."""
    element = getattr(hlt, "heater", None)
    if not element:
        return None
    for k in (cbpi.kettle.data or []):
        if k is not None and k.heater == element and _on(k):
            return "own" if k is hlt else "herms"
    return "hand" if _on(cbpi.actor.find_by_id(element)) else None


def reached_sparge(cbpi):
    """Has the recipe reached its runoff: the first step named for it
    (StepController._collects_wort), or any after it, started, paused, failed
    or done? A recipe with no such step never does, and nor does one that is
    over - every step done, which the brew-day screen shows as no recipe
    running: heat after that is brewing by hand."""
    steps = getattr(cbpi, "step", None)
    collects = getattr(steps, "_collects_wort", None)
    profile = list(getattr(steps, "profile", None) or [])
    if not callable(collects) or all(getattr(s, "status", None) == StepState.DONE for s in profile):
        return False
    for i, step in enumerate(profile):
        if collects(step):
            return any(getattr(s, "status", StepState.INITIAL) != StepState.INITIAL for s in profile[i:])
    return False


def hlt_pump(cbpi):
    """The HLT and its pump's id, if this extension may run that pump; else (hlt or None, None)."""
    hlt = find_hlt(cbpi)
    pump_id = getattr(hlt, "agitator", None) if hlt is not None else None
    if not pump_id:
        return hlt, None
    if any(k is not hlt and k.agitator == pump_id for k in (cbpi.kettle.data or []) if k is not None):
        return hlt, None
    return hlt, pump_id


async def pump_off(cbpi, pump_id):
    """Switch a pump off if it is running. True if it was, and the switch
    answered; never raises."""
    pump = cbpi.actor.find_by_id(pump_id) if pump_id else None
    if pump is None or not _on(pump):
        return False
    try:
        return await cbpi.actor.off(pump_id) is not False
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not switch %s off: %s", getattr(pump, "name", pump_id), e)
        return False


async def runoff_pumps_off(cbpi):
    """The runoff is over: switch the wort pump (the mash tun's agitator) and
    the HLT pump (the HLT's) off, whichever are running, so the brewer can move
    their hoses. The boil kettle's own pump is left alone. Returns the words
    for those switched off, the wort pump first."""
    roles = kettle_roles(cbpi)
    done = []
    for word, kettle in (("the wort pump", roles["mt"]), ("the HLT pump", roles["hlt"])):
        pump_id = getattr(kettle, "agitator", None) if kettle is not None else None
        if pump_id and await pump_off(cbpi, pump_id):
            done.append(word)
    return done


class HLTPumpWithHeat(CBPiExtension):

    def __init__(self, cbpi, watch=True):
        self.cbpi = cbpi
        self._source = None
        # The switch-off as the runoff ends, while it runs: for a test to wait on.
        self._runoff_task = None
        add = getattr(getattr(cbpi, "step", None), "add_listener", None)
        if callable(add):
            add(self.step_event)
        self._task = asyncio.create_task(self.run()) if watch else None

    def enabled(self):
        return str(_setting(self.cbpi, SETTING, "Yes")) == "Yes"

    async def run(self):
        # Brewing time, like the steps' own loops, so a simulator at any time
        # scale switches the pump at the same brewing moment.
        while True:
            await clock.sleep(1)
            try:
                await self.look()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad look must not end the watch
                logger.warning("HLT pump: could not check the HLT: %s", e)

    async def look(self):
        """Switch the HLT pump on if the HLT has just started heating, before the
        sparge."""
        if not self.enabled():
            self._source = None
            return
        hlt, pump_id = hlt_pump(self.cbpi)
        now = heat_source(self.cbpi, hlt) if hlt is not None else None
        started, self._source = now is not None and now != self._source, now
        if not started or not pump_id or reached_sparge(self.cbpi):
            return
        pump = self.cbpi.actor.find_by_id(pump_id)
        if pump is None or _on(pump):
            return
        logger.info("HLT pump on: the HLT is heating (%s), before the sparge (%s)", now, SETTING)
        if await self.cbpi.actor.on(pump_id) is False:
            return
        try:
            self.cbpi.notify("HLT pump", STARTED_TEXT.format(SETTING), NotificationType.INFO)
        except Exception as e:  # noqa: BLE001 - saying why never stops the pump
            logger.debug("HLT pump: could not say why it started: %s", e)

    def step_event(self, event, snapshot):
        """A step listener (StepController.add_listener).

        When a BrewStart step starts - the brew set up to start later by
        itself - ask for the HLT pump and its valves to be checked, kept on
        every page until answered, or until that step is stopped or over.

        When a step named for the runoff ends, and the next step is not one,
        switch the HLT pump off (see the module's notes)."""
        if event == "step_done":
            self._runoff_ended(snapshot or {})
            return
        if event != "step_started" or not self.enabled():
            return
        step_id = (snapshot or {}).get("step_id")
        step = next((s for s in (snapshot or {}).get("steps") or [] if s.get("id") == step_id), None)
        if step is None or step.get("type") not in SCHEDULED_STEPS:
            return
        if hlt_pump(self.cbpi)[1] is None:
            return
        notification_id = self.cbpi.notify(CHECK_TITLE, CHECK_TEXT.format(SETTING), NotificationType.WARNING,
                                           action=[NotificationAction("Checked")], keep=True)
        keep = getattr(self.cbpi.step, "keep_step_prompt", None)
        if notification_id and callable(keep):
            keep(step_id, "hlt-pump-check", notification_id)

    def _runoff_ended(self, snapshot):
        """A step named for the runoff has ended: switch the HLT pump off, unless
        the next step carries the runoff on."""
        step_id = snapshot.get("step_id")
        step = next((s for s in snapshot.get("steps") or [] if s.get("id") == step_id), None)
        collects = getattr(getattr(self.cbpi, "step", None), "_collects_wort", None)
        if step is None or not callable(collects):
            return
        if not collects(types.SimpleNamespace(name=step.get("name"), type=step.get("type"))):
            return
        if self._runoff_goes_on(step_id, collects):
            return
        self._runoff_task = asyncio.ensure_future(self._end_runoff(step.get("name") or "The runoff step"))

    def _runoff_goes_on(self, step_id, collects):
        """Is the step after `step_id` in the profile also named for the runoff?"""
        profile = list(getattr(getattr(self.cbpi, "step", None), "profile", None) or [])
        index = next((i for i, s in enumerate(profile) if getattr(s, "id", None) == step_id), None)
        if index is None or index + 1 >= len(profile):
            return False
        try:
            return bool(collects(profile[index + 1]))
        except Exception:  # noqa: BLE001 - unknown: the runoff is taken as over
            return False

    async def _end_runoff(self, name):
        try:
            _, pump_id = hlt_pump(self.cbpi)
            pump = self.cbpi.actor.find_by_id(pump_id) if pump_id else None
            if pump is None or not _on(pump):
                return
            logger.info("HLT pump off: %s has ended, so the runoff is over", name)
            if not await pump_off(self.cbpi, pump_id):
                self.cbpi.notify(RUNOFF_FAILED_TITLE, RUNOFF_FAILED_TEXT.format(name), NotificationType.WARNING,
                                 action=[NotificationAction("OK")], keep=True)
                return
            text = RUNOFF_OFF_TEXT.format(name)
            mt = kettle_roles(self.cbpi)["mt"]
            wort = self.cbpi.actor.find_by_id(mt.agitator) if mt is not None and mt.agitator else None
            if wort is not None and _on(wort):
                text += " " + RUNOFF_WORT_TEXT
            self.cbpi.notify(RUNOFF_TITLE, text, NotificationType.INFO)
        except Exception as e:  # noqa: BLE001 - the brew goes on whatever this does
            logger.warning("HLT pump: could not switch it off as the runoff ended: %s", e)


def setup(cbpi):
    cbpi.plugin.register("HLTPumpWithHeat", HLTPumpWithHeat)
