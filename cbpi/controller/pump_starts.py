"""No pump starts by itself: a step, a kettle logic or an extension that needs one running asks the brewer for it.

A pump is any kettle's agitator: on a three-vessel HERMS the HLT pump (the HLT's), which stirs the HLT and moves the
sparge water, and the wort pump (the mash tun's), which recirculates the mash through the coil in the HLT. A pump
started with its hoses or valves set wrong can empty a vessel, run dry, or send hot wort where nobody expects it, so
only the brewer starts one: his own press of it, or Start pump on the prompt that asks for it.

- His start stands until he stops it again (his OFF, ALL OFF), STOP pauses the brew, the pump is stopped for safety
  (pump_stopped: before hoses move at the sparge and the boil, as the runoff and the recipe end, and the mash-in's
  hold for the dough-in), or the brew starts over (a reset, a load, an unload). While it stands, a logic may run the
  pump - switch it off and on again for its rests - and the step after the one he started it in carries on with it.
- A logic that wants a pump he hasn't started asks for it (may_start_pump): a prompt titled with the step's name,
  "Start the wort pump to recirculate.", kept on every page until answered, with its button Start pump, which
  switches the pump on. His own press of the pump answers it too. Asked once a step: the step it is asked in takes
  it down as it ends, and a logic asking again in the same step asks nothing. Until he starts it, nothing that needs
  the pump runs: PID_HERMS, for one, sends no heat through the coil.
- A pump already running when it's wanted - his, or a ToggleStep's he put in the recipe - is taken as started.
- Stopped by his own press during a step, it isn't asked for again in that step: the step's second line says it's
  off, quietly. The next step asks again, once.
- The answer is still a press of the pump: before the strike water is said to be in the mash tun, the wort pump
  stays off whatever he answers (StepController.strike_move_refuses).

The one exception is BrewStart heating the strike water, which may run the HLT pump by itself (cbpi/extension/hltpump,
HLT_PUMP_WITH_HEAT). Steps the brewer puts in his recipe to switch an actor - ToggleStep, ActorStep - switch it, as
he asked. Kept in memory only: after a restart, a pump is asked for again.
"""

import inspect
import logging

from cbpi.api import presence
from cbpi.api.dataclasses import NotificationAction, NotificationType
from cbpi.api.step import StepState


class PumpStarts:
    """The step controller's part: which pumps the brewer has started, and asking for one (see the module)."""

    #: The prompt's button.
    PUMP_START = "Start pump"
    #: The prompt: "Start the wort pump to recirculate."; for a pump nothing says the purpose of, "Start Kettle Pump."
    PUMP_ASK_TEXT = "Start {pump} to {purpose}."
    PUMP_ASK_PLAIN = "Start {pump}."
    #: The step's second line while a pump he stopped during it is wanted: "Wort pump off. Start it to recirculate."
    PUMP_OFF_LINE = "{pump} off. Start it to {purpose}."
    PUMP_OFF_PLAIN = "{pump} off. Start it when you're ready."
    #: What each pump is asked for, by its role, when the caller doesn't say.
    PUMP_PURPOSE = {"wort": "recirculate", "hlt": "stir the HLT"}
    #: The kind a page can tell the prompt by (NotificationController.notify).
    PUMP_KIND = "pump_start"

    def _pump_book(self):
        """What this controller keeps about pumps, made on first use, so a controller built without __init__ has it:
        started (pump ids his start stands for), asked ({pump id: (step id or None, notification id)}), stopped
        ({pump id: step id or None he stopped it in}), line ({pump id: (step id, the line)})."""
        return self.__dict__.setdefault("_pump_starts", {"started": set(), "asked": {}, "stopped": {}, "line": {}})

    def _pump_kettles(self):
        kettles = getattr(getattr(self.cbpi, "kettle", None), "data", None) or []
        return [k for k in kettles if k is not None]

    def pump_role(self, actor_id):
        """"wort" for the mash tun's agitator, "hlt" for the HLT's, "pump" for another kettle's; None for an actor
        that is no kettle's agitator. Never raises."""
        if not actor_id:
            return None
        try:
            roles = self._kettle_roles()
            for role, key in (("wort", "mt"), ("hlt", "hlt")):
                kettle = roles.get(key)
                if kettle is not None and getattr(kettle, "agitator", None) == actor_id:
                    return role
            if any(getattr(k, "agitator", None) == actor_id for k in self._pump_kettles()):
                return "pump"
        except Exception as e:  # noqa: BLE001
            logging.debug("Could not tell whether %s is a pump: %s", actor_id, e)
        return None

    def is_pump(self, actor_id):
        """Whether `actor_id` is a pump: some kettle's agitator."""
        return self.pump_role(actor_id) is not None

    def _pump_words(self, pump_id):
        """"the wort pump", "the HLT pump", else the actor's name."""
        role = self.pump_role(pump_id)
        if role == "wort":
            return "the wort pump"
        if role == "hlt":
            return "the HLT pump"
        actor = self._pump_actor(pump_id)
        return str(getattr(actor, "name", None) or "the pump")

    @staticmethod
    def _pump_noun(words):
        """"the wort pump" -> "Wort pump", "the HLT pump" -> "HLT pump": a title, or the start of a line."""
        words = words[4:] if words.startswith("the ") else words
        return words[:1].upper() + words[1:]

    def _pump_actor(self, pump_id):
        try:
            return self.cbpi.actor.find_by_id(pump_id)
        except Exception:  # noqa: BLE001
            return None

    def _pump_running(self, pump_id):
        actor = self._pump_actor(pump_id)
        return getattr(getattr(actor, "instance", None), "state", False) is True

    def _pump_step(self):
        """The step running now, or None."""
        try:
            return self.find_by_status(StepState.ACTIVE) if getattr(self, "profile", None) else None
        except Exception:  # noqa: BLE001
            return None

    def pump_started(self, pump_id):
        """Whether `pump_id` may run now as far as the brewer is concerned: his start stands, or it is running."""
        return pump_id in self._pump_book()["started"] or self._pump_running(pump_id)

    async def may_start_pump(self, pump_id, purpose=None, quiet=False):
        """True when a step, a logic or an extension may switch `pump_id` on now: the brewer has started it and that
        stands, or it is running already (taken as started). Otherwise False, having asked him for it, once a step;
        with `quiet`, when he stopped it himself during this step, the step's second line says it's off instead.
        `purpose`: what it is for, as the prompt says it ("recirculate"). Anything that isn't a pump: True. Never
        raises: not knowing is no permission."""
        try:
            if not pump_id or not self.is_pump(pump_id):
                return True
            book = self._pump_book()
            if pump_id in book["started"]:
                self._clear_pump_line(pump_id)
                return True
            if self._pump_running(pump_id):
                book["started"].add(pump_id)
                logging.info("%s is running already, so it may go on running", self._pump_words(pump_id))
                return True
            step = self._pump_step()
            key = getattr(step, "id", None)
            purpose = purpose or self.PUMP_PURPOSE.get(self.pump_role(pump_id))
            if key is not None and book["stopped"].get(pump_id) == key:
                if quiet:
                    self._show_pump_line(step, pump_id, purpose)
                return False
            asked = book["asked"].get(pump_id)
            if asked is not None and asked[0] == key:
                return False
            self._ask_for_pump(step, pump_id, purpose)
            return False
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not tell whether %s may start, so it stays off: %s", pump_id, e)
            return False

    def _ask_for_pump(self, step, pump_id, purpose):
        words = self._pump_words(pump_id)
        text = (self.PUMP_ASK_TEXT if purpose else self.PUMP_ASK_PLAIN).format(pump=words, purpose=purpose)
        title = getattr(step, "name", None) or self._pump_noun(words)

        async def start_pressed():
            await self._start_pump_pressed(pump_id)

        logging.info("Asking the brewer to start %s (%s)", words, getattr(step, "name", None) or "no step running")
        try:
            notification_id = self.cbpi.notify(
                title, text, NotificationType.INFO if step is not None else NotificationType.WARNING,
                action=[NotificationAction(self.PUMP_START, start_pressed)], keep=True, kind=self.PUMP_KIND)
        except TypeError:
            notification_id = self.cbpi.notify(
                title, text, NotificationType.INFO if step is not None else NotificationType.WARNING,
                action=[NotificationAction(self.PUMP_START, start_pressed)])
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not ask for %s: %s", words, e)
            return
        key = getattr(step, "id", None)
        self._pump_book()["asked"][pump_id] = (key, notification_id)
        if step is not None and notification_id:
            # Taken down from every page once the step is paused or ends.
            self.keep_step_prompt(step.id, "pump:{}".format(pump_id), notification_id)

    async def _start_pump_pressed(self, pump_id):
        """Start pump: switch it on, as his press. Refused - the strike water isn't in yet, or the interlock - it is
        asked for again the next time it's wanted; what refused it says why."""
        ok = await self.cbpi.actor.on(pump_id)
        book = self._pump_book()
        if ok is not False and self._pump_running(pump_id):
            self._pump_started_by_brewer(pump_id, "Start pump")
            return
        book["asked"].pop(pump_id, None)
        logging.warning("Start pump: %s did not start", self._pump_words(pump_id))

    def _pump_started_by_brewer(self, pump_id, how):
        book = self._pump_book()
        book["started"].add(pump_id)
        book["stopped"].pop(pump_id, None)
        self._clear_pump_line(pump_id)
        logging.info("The brewer started %s (%s)", self._pump_words(pump_id), how)

    def pump_pressed(self, pump_id, on):
        """The brewer's own press of a pump's ON (`on` True) or OFF (ActorController). ON starts it as Start pump
        does, and answers a prompt asking for it; OFF stops it until he starts it again, and it isn't asked for
        again in this step. Never raises."""
        try:
            if not self.is_pump(pump_id):
                return
            book = self._pump_book()
            if on:
                self._pump_started_by_brewer(pump_id, "his press")
                asked = book["asked"].get(pump_id)
                if asked is not None and asked[1]:
                    self._retire_prompt(asked[1])
                return
            book["started"].discard(pump_id)
            step = self._pump_step()
            # Not asked for again in the step he stopped it in. With no step running there is no such step: what
            # asked for it then is asked again only when it starts again (withdraw_pump_ask).
            if step is not None:
                book["stopped"][pump_id] = step.id
            logging.info("The brewer stopped %s: nothing starts it again until he does", self._pump_words(pump_id))
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not note the brewer's press of %s: %s", pump_id, e)

    def pump_stopped(self, pump_id):
        """`pump_id` has been switched off for safety - before hoses move, as the runoff or the recipe ends, held for
        the dough-in: the brewer's start no longer stands, and the next step, or the next want in this one, asks
        afresh. Never raises."""
        try:
            if not pump_id:
                return
            book = self._pump_book()
            book["started"].discard(pump_id)
            book["stopped"].pop(pump_id, None)
            asked = book["asked"].pop(pump_id, None)
            if asked is not None and asked[1]:
                self._retire_prompt(asked[1])
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not note that %s was stopped: %s", pump_id, e)

    def withdraw_pump_ask(self, pump_id):
        """Nothing wants `pump_id` any more - the logic asking for it has stopped: its prompt comes down, and it is
        asked afresh when it is wanted again. Never raises."""
        try:
            asked = self._pump_book()["asked"].pop(pump_id, None) if pump_id else None
            if asked is not None and asked[1]:
                self._retire_prompt(asked[1])
            self._clear_pump_line(pump_id)
        except Exception as e:  # noqa: BLE001
            logging.debug("Could not take the prompt for %s down: %s", pump_id, e)

    def _forget_pump_starts(self):
        """STOP, ALL OFF, a reset, a load or an unload: no start of the brewer's stands, and every pump is asked for
        afresh."""
        book = self._pump_book()
        for pump_id in list(book["asked"]):
            self.withdraw_pump_ask(pump_id)
        for pump_id in list(book["line"]):
            self._clear_pump_line(pump_id)
        book["started"].clear()
        book["stopped"].clear()

    def _show_pump_line(self, step, pump_id, purpose):
        """The step's second line, quietly, while a pump the brewer stopped is wanted: once, and only where the step
        isn't using that line itself."""
        instance = getattr(step, "instance", None)
        if instance is None:
            return
        words = self._pump_words(pump_id)
        line = (self.PUMP_OFF_LINE if purpose else self.PUMP_OFF_PLAIN).format(pump=self._pump_noun(words),
                                                                               purpose=purpose)
        book = self._pump_book()
        current = getattr(instance, "summary2", None)
        if current == line:
            return
        if current not in (None, ""):
            return
        instance.summary2 = line
        book["line"][pump_id] = (step.id, line)
        try:
            self.push_udpate()
        except Exception as e:  # noqa: BLE001
            logging.debug("Could not show that %s is off: %s", words, e)

    def _clear_pump_line(self, pump_id):
        shown = self._pump_book()["line"].pop(pump_id, None)
        if shown is None:
            return
        step = self.find_by_id(shown[0]) if getattr(self, "profile", None) else None
        instance = getattr(step, "instance", None)
        if instance is not None and getattr(instance, "summary2", None) == shown[1]:
            instance.summary2 = None
            try:
                self.push_udpate()
            except Exception as e:  # noqa: BLE001
                logging.debug("Could not take the pump's line down: %s", e)


def pressed_pump(cbpi, actor_id, on):
    """For ActorController: tell the step controller about the brewer's own press of `actor_id`'s ON or OFF, if this
    is his press (cbpi.api.presence). Never raises."""
    if presence.pressed() != "actor":
        return
    told = getattr(getattr(cbpi, "step", None), "pump_pressed", None)
    if callable(told) and not inspect.iscoroutinefunction(told):
        told(actor_id, on)
