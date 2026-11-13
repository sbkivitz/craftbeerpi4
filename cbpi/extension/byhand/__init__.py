"""Raise an alarm when an element heats by hand past its target, or near the boil.

In MANUAL nothing compares the temperature with the target, so an element
switched on by hand runs until someone turns it off: a boil kettle set to 150
for a brew in a bag reaches 212, and an HLT element switched on without AUTO
heats without limit. MANUAL stays MANUAL: taking over by hand is what it is
for. The brew-day screen says so under the vessel's window, and this raises an
alarm on every page, with no page open too.

The rules, as the brew-day design sets them out (craftbeerpi4-ui,
design/brewday/BUILD-NOTES.md, "Heating by hand"):

- By hand: the element is on and no AUTO runs it: not its own kettle's, and
  for the HLT not the mash tun's HERMS either. The kettles are found as the
  brew-day screen finds them (live.js kettleRoles): the HLT always; the boil
  kettle only with a target below BOIL, a temperature to hold, and a BoilPower
  that holds one (it declares set_boil_at); never the mash tun, which has no
  element of its own. Boiling by hand is what a boil kettle is for.
- The alarm comes once the vessel has been, for AFTER brewing seconds, either
  2 F (1 C) or more past its target, or at or past where BOIL starts: the boil
  kettle's Boil_Threshold less 3.6 F (2 C). A target at or past where BOIL
  starts counts as none: the water boils before it gets there.
- At most twice in one stretch of heating by hand: once past the target, once
  near the boil. A stretch ends when the element goes off, the kettle's AUTO
  comes on, or its target changes.
- An error with no buttons, titled with the kettle's name. The brew-day screen
  puts errors first, so it beeps even with a hop alert waiting. It isn't kept
  for a page opened later: the line under the window says the same there, for
  as long as it's true.
"""

import asyncio
import logging
import math
import re

from cbpi.api import CBPiExtension, clock
from cbpi.api.dataclasses import NotificationType

logger = logging.getLogger(__name__)

#: Brewing seconds past the target, or near the boil, before the alarm.
AFTER = 30

#: How far past its target a vessel heated by hand may go, in the unit's degrees.
PAST = {"F": 2.0, "C": 1.0}
#: BoilPower lets go of a boil this far below its boil point (BOIL_EXIT_DROP_C),
#: and the same margin is where a target stops being BOIL: where BOIL starts.
MARGIN = {"F": 3.6, "C": 2.0}
#: BoilPower's boil point when the boil kettle has none set.
BOIL_POINT = {"F": 210.2, "C": 99.0}

PAST_TEXT = ("Above its {target} target and still heating: its element is on by hand, and in MANUAL nothing stops it "
             "at the target. Turn the element off, or put the {name} in AUTO to hold {target}.")
BOIL_TEXT = ("Near the boil and still heating: its element is on by hand, and in MANUAL nothing stops it before it "
             "boils. Turn the element off.")

# The target of a stretch with no element on by hand: never equal to one.
_NONE = object()


def number(value):
    """A finite number, as the brew-day screen reads one (live.js num), else None."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        n = float(value) if isinstance(value, (int, float)) else float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _prop(kettle, key):
    props = getattr(kettle, "props", None)
    try:
        return props.get(key) if props is not None else None
    except Exception:  # noqa: BLE001
        return None


def _auto(kettle):
    return kettle is not None and getattr(getattr(kettle, "instance", None), "state", False) is True


def _types(cbpi):
    return getattr(getattr(cbpi, "kettle", None), "types", None) or {}


def _herms_logics(cbpi):
    """The kettle logics that run a HERMS: those with an HLT_Sensor setting, as PID_HERMS has."""
    return {name for name, t in _types(cbpi).items()
            if any(isinstance(p, dict) and p.get("label") == "HLT_Sensor" for p in (t or {}).get("properties") or [])}


def _declares(cbpi, logic, method):
    t = _types(cbpi).get(logic) or {}
    return any(isinstance(a, dict) and a.get("method") == method for a in t.get("actions") or [])


def roles(cbpi):
    """{"hlt", "mt", "bk"}: each kettle, or None, as the brew-day screen finds
    them (live.js kettleRoles)."""
    kettles = [k for k in (getattr(getattr(cbpi, "kettle", None), "data", None) or []) if k is not None]
    herms = _herms_logics(cbpi)
    config = getattr(cbpi, "config", None)

    def setting(key):
        try:
            return config.get(key, None) if config is not None else None
        except Exception:  # noqa: BLE001
            return None

    def by_id(kettle_id):
        return next((k for k in kettles if kettle_id and k.id == kettle_id), None)

    def named(pattern, among):
        return next((k for k in among if re.search(pattern, str(getattr(k, "name", "") or ""), re.I)), None)

    def is_herms(k):
        return k.type in herms if herms else bool(re.search("herms", str(k.type or ""), re.I))

    mt = by_id(setting("MASH_TUN")) or next((k for k in kettles if is_herms(k)), None) or named("mash", kettles)
    others = [k for k in kettles if k is not mt]
    bk = (by_id(setting("BoilKettle")) or named("boil", others)
          or next((k for k in others if re.search("boil", str(k.type or ""), re.I)), None))
    rest = [k for k in kettles if k is not mt and k is not bk]
    hlt_kettle, hlt_sensor = (_prop(mt, "HLT_Kettle"), _prop(mt, "HLT_Sensor")) if mt is not None else (None, None)
    hlt = ((hlt_kettle and next((k for k in rest if k.id == hlt_kettle), None))
           or (hlt_sensor and next((k for k in rest if k.sensor == hlt_sensor), None))
           or (mt is not None and mt.heater and next((k for k in rest if k.heater == mt.heater), None))
           or named(r"\bhlt\b|hot liquor", rest)
           or (rest[0] if len(rest) == 1 else None))
    return {"hlt": hlt or None, "mt": mt if mt is not bk else None, "bk": bk or None}


class HeatingByHand(CBPiExtension):

    def __init__(self, cbpi, watch=True):
        self.cbpi = cbpi
        # Per role ("hlt", "bk"): the stretch of heating by hand - its kettle,
        # its target, what has been raised in it, and since when it has been
        # past its target or near the boil with no alarm for that yet.
        self._stretch = {}
        self._task = asyncio.create_task(self.run()) if watch else None

    async def run(self):
        # Brewing time, so a simulator at any speed raises it at the same brewing moment.
        while True:
            await clock.sleep(1)
            try:
                self.look()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad look must not end the watch
                logger.warning("Heating by hand: could not check the kettles: %s", e)

    def unit(self):
        try:
            unit = str(self.cbpi.config.get("TEMP_UNIT", "C") or "C").strip().upper()
        except Exception:  # noqa: BLE001
            unit = "C"
        return "F" if unit.startswith("F") else "C"

    def boil_from(self, bk, unit):
        """Where BoilPower boils (the screen's boilFrom): the boil kettle's
        Boil_Threshold, or BoilPower's default. An older BoilPower, without
        set_boil_at, takes the kettle's target as its boil point."""
        default = BOIL_POINT[unit]
        if bk is None:
            return default
        threshold = number(_prop(bk, "Boil_Threshold"))
        if not _declares(self.cbpi, bk.type, "set_boil_at"):
            target = number(getattr(bk, "target_temp", None))
            if target is not None and target > 0:
                return target
        return default if threshold is None else threshold

    def _on(self, actor_id):
        if not actor_id:
            return False
        try:
            actor = self.cbpi.actor.find_by_id(actor_id)
        except Exception:  # noqa: BLE001
            return False
        return getattr(getattr(actor, "instance", None), "state", False) is True

    def _reading(self, kettle):
        try:
            value = self.cbpi.sensor.get_sensor_value(kettle.sensor)
        except Exception:  # noqa: BLE001
            return None
        return number(value.get("value") if isinstance(value, dict) else value)

    def heating_by_hand(self, role, k, unit, let_go):
        """{"kettle", "target", "over"} while `role`'s element heats by hand,
        else None. target: the one it won't stop at, or None (none, or one at
        or past where BOIL starts). over: "boil", "target" or ""."""
        kettle = k.get(role)
        if kettle is None:
            return None
        raw = number(getattr(kettle, "target_temp", None))
        if role == "hlt":
            heater = kettle.heater or (k["mt"].heater if k["mt"] is not None else None)
            if _auto(kettle) or _auto(k["mt"]):
                return None
        else:
            heater = kettle.heater
            if _auto(kettle) or not _declares(self.cbpi, kettle.type, "set_boil_at"):
                return None
            if raw is None or not 0 < raw < let_go:
                return None
        if not self._on(heater):
            return None
        target = raw if raw is not None and 0 < raw < let_go else None
        reading = self._reading(kettle)
        over = ""
        if reading is not None:
            if reading >= let_go:
                over = "boil"
            elif target is not None and reading >= target + PAST[unit]:
                over = "target"
        return {"kettle": kettle, "target": target, "over": over}

    def look(self, now=None):
        """Raise the alarm for each element that has heated by hand past its
        target, or near the boil, for AFTER brewing seconds."""
        now = clock.now() if now is None else now
        unit = self.unit()
        k = roles(self.cbpi)
        let_go = round(self.boil_from(k["bk"], unit) - MARGIN[unit], 1)
        for role in ("hlt", "bk"):
            w = self.heating_by_hand(role, k, unit, let_go)
            kettle_id = w["kettle"].id if w else None
            target = w["target"] if w else _NONE
            s = self._stretch.get(role)
            if s is None or s["kettle"] != kettle_id or s["target"] != target:
                s = self._stretch[role] = {"kettle": kettle_id, "target": target, "raised": set(), "since": None}
            if w is None or not w["over"] or w["over"] in s["raised"]:
                s["since"] = None
                continue
            if s["since"] is None:
                s["since"] = now
            if now - s["since"] < AFTER:
                continue
            s["raised"].add(w["over"])
            s["since"] = None
            self._raise(w, unit)

    def _raise(self, w, unit):
        kettle = w["kettle"]
        name = getattr(kettle, "name", None) or "Kettle"
        if w["over"] == "target":
            shown = "{:.1f}".format(w["target"])
            shown = shown[:-2] if shown.endswith(".0") else shown
            text = PAST_TEXT.format(target="{} °{}".format(shown, unit), name=name)
        else:
            text = BOIL_TEXT
        # INFO, not WARNING: with NOTIFY_ON_ERROR on, a logged warning becomes
        # a second notification with the same words.
        logger.info("Heating by hand: %s: %s", name, text)
        try:
            self.cbpi.notify(name, text, NotificationType.ERROR)
        except Exception as e:  # noqa: BLE001 - an alarm never stops a kettle
            logger.warning("Heating by hand: could not raise the alarm for %s: %s", name, e)


def setup(cbpi):
    cbpi.plugin.register("HeatingByHand", HeatingByHand)
