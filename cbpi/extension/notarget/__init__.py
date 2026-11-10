"""Warn when a kettle's AUTO runs with no target.

Scott, 10 Nov, on the simulator: "What went wrong? I have Mash Tun "Auto" on
but HLT is off. I think a "SV" was not set - but it should pop up a warning in
that case". The mash tun's target was 0, left from the last recipe. PID_HERMS
takes 0 as a real target, so it held the mash at 0 °F, never asked the HLT for
heat, and nothing said so. PIDBoil and Hysteresis do the same. BoilPower is
the one logic that runs without one: as it starts, it puts its boil threshold
in as the kettle's target.

So, on the server, with no page open too: when a kettle's AUTO has run for
GRACE brewing seconds with no target - none, 0 or below, or not a number - a
warning with an OK button, kept for every page until answered, as "HLT pump
still running" is. It is taken down by itself once the kettle has a target or
its AUTO is off. Answered, it is not asked again until the kettle next runs
AUTO with no target. GRACE lets whatever starts a kettle set its target first:
every step does, and so does BoilPower.
"""

import asyncio
import logging
import math

from cbpi.api import CBPiExtension, clock
from cbpi.api.dataclasses import NotificationAction, NotificationType

logger = logging.getLogger(__name__)

#: Brewing seconds a kettle may run AUTO with no target before the warning.
GRACE = 5

TITLE = "No target for the {}"
TEXT = ("AUTO is on, but it has no target temperature, so it won't heat. "
        "Set a target, or switch it to MANUAL.")
#: For a mash tun whose logic runs a HERMS: what it heats with is the HLT.
TEXT_HERMS = ("AUTO is on, but it has no target temperature, so the HLT won't heat for it. "
              "Set a target, or switch it to MANUAL.")


def has_no_target(kettle):
    """No target: none, 0 or below, or not a finite number."""
    try:
        value = float(getattr(kettle, "target_temp", None))
    except (TypeError, ValueError):
        return True
    return not math.isfinite(value) or value <= 0


def _running(kettle):
    return getattr(getattr(kettle, "instance", None), "state", False) is True


def _herms_logics(cbpi):
    """The kettle logics that run a HERMS: those with an HLT_Sensor setting, as PID_HERMS has."""
    found = set()
    for name, t in (getattr(cbpi.kettle, "types", None) or {}).items():
        props = (t or {}).get("properties") or []
        if any(isinstance(p, dict) and p.get("label") == "HLT_Sensor" for p in props):
            found.add(name)
    return found


class NoTargetWarning(CBPiExtension):

    def __init__(self, cbpi, watch=True):
        self.cbpi = cbpi
        # Per kettle id: the brewing time it began running AUTO with no target.
        self._since = {}
        # Per kettle id, once warned: the warning's id, or None once answered.
        self._warned = {}
        self._task = asyncio.create_task(self.run()) if watch else None

    async def run(self):
        # Brewing time, so a simulator at any speed warns at the same brewing moment.
        while True:
            await clock.sleep(1)
            try:
                self.look()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad look must not end the watch
                logger.warning("No-target warning: could not check the kettles: %s", e)

    def look(self, now=None):
        """Warn for each kettle that has run AUTO with no target for GRACE; take
        the warning down for each that no longer does."""
        now = clock.now() if now is None else now
        kettles = {k.id: k for k in (getattr(self.cbpi.kettle, "data", None) or []) if k is not None}
        for kettle_id in list(self._since):
            kettle = kettles.get(kettle_id)
            if kettle is None or not _running(kettle) or not has_no_target(kettle):
                self._end(kettle_id)
        waiting = self._waiting()
        for kettle_id, kettle in kettles.items():
            if not _running(kettle) or not has_no_target(kettle):
                continue
            since = self._since.setdefault(kettle_id, now)
            if kettle_id in self._warned:
                notification_id = self._warned[kettle_id]
                if notification_id is not None and waiting is not None and notification_id not in waiting:
                    self._warned[kettle_id] = None
                continue
            if now - since >= GRACE:
                self._warn(kettle)

    def _waiting(self):
        waiting = getattr(getattr(self.cbpi, "notification", None), "waiting_prompts", None)
        if not callable(waiting):
            return None
        try:
            return set(waiting() or [])
        except Exception:  # noqa: BLE001
            return None

    def _warn(self, kettle):
        text = TEXT_HERMS if kettle.type in _herms_logics(self.cbpi) else TEXT
        name = getattr(kettle, "name", None) or "kettle"
        logger.info("No target for %s: its AUTO is on with target %r", name, getattr(kettle, "target_temp", None))
        notification_id = None
        try:
            notification_id = self.cbpi.notify(TITLE.format(name), text, NotificationType.WARNING,
                                               action=[NotificationAction("OK")], keep=True)
        except Exception as e:  # noqa: BLE001 - a warning never stops a kettle
            logger.warning("No-target warning: could not warn about %s: %s", name, e)
        self._warned[kettle.id] = notification_id

    def _end(self, kettle_id):
        self._since.pop(kettle_id, None)
        notification_id = self._warned.pop(kettle_id, None)
        if notification_id is None:
            return
        retire = getattr(getattr(self.cbpi, "notification", None), "retire", None)
        if callable(retire):
            try:
                retire(notification_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("No-target warning: could not take %s down: %s", notification_id, e)


def setup(cbpi):
    cbpi.plugin.register("NoTargetWarning", NoTargetWarning)
