import asyncio
import inspect
import logging
import math
import time
import warnings
from datetime import datetime
from socket import timeout
from typing import KeysView

import numpy as np
from cbpi.api import *
from cbpi.api import Property, action, parameters
from cbpi.api import clock
from cbpi.api.base import CBPiBase
from cbpi.api.config import ConfigType
from cbpi.api.dataclasses import Kettle, NotificationAction, NotificationType, Props
from cbpi.api.strike import strike_temperature
from cbpi.api.step import CBPiStep, StepResult, StepState
from cbpi.api.timer import Timer
from voluptuous.schema_builder import message


def _completion_time(timer):
    """Wall-clock HH:MM at which a timer that has just started reaches zero.

    From what the timer will actually run. These notifications added the
    step's configured length, so a rest resumed after a power cut with forty
    minutes left announced an end twenty minutes late (DEFECTS M20). Divided
    by the clock's scale so a simulated brew day names a time on the clock it
    is watched by; on a rig the scale is 1.

    Never raises. It runs inside the step's run() loop, just after the timer
    starts, and an exception here ended the step: an end past what the
    calendar holds raised OSError on Windows, and so did any timer without
    seconds_left() (DEFECTS M23). A courtesy must not cost the rest.
    """
    try:
        seconds = timer.seconds_left() / clock.scale()
        return datetime.fromtimestamp(time.time() + seconds).strftime("%H:%M")
    except Exception as e:  # noqa: BLE001
        logging.warning("Could not work out when the step ends: %s", e)
        return "unknown"


@parameters(
    [
        Property.Text(
            label="Notification", configurable=True, description="Text for notification"
        ),
        Property.Select(
            label="AutoNext",
            options=["Yes", "No"],
            # Declared, so a step made in an editor saves what the step does
            # when it is missing: it pauses (DEFECTS M41).
            default_value="No",
            description="Automatically move to next step (Yes) or pause after Notification (No)",
        ),
    ]
)
class NotificationStep(CBPiStep):

    async def NextStep(self, **kwargs):
        await self.next()

    async def on_timer_done(self, timer):
        self.summary = self.props.get("Notification", "")

        if self.AutoNext == True:
            self.cbpi.notify(
                self.name, self.props.get("Notification", ""), NotificationType.INFO
            )
            await self.next()
        else:
            self.cbpi.notify(
                self.name,
                self.props.get("Notification", ""),
                NotificationType.INFO,
                action=[NotificationAction("Next Step", self.NextStep)],
            )
            await self.push_update()

    async def on_timer_update(self, timer, seconds):
        await self.push_update()

    async def on_start(self):
        self.summary = ""
        self.AutoNext = False if self.props.get("AutoNext", "No") == "No" else True
        if self.timer is None:
            self.timer = Timer(
                1, on_update=self.on_timer_update, on_done=self.on_timer_done
            )
        await self.push_update()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        await self.push_update()

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
            if self.timer.is_running is not True:
                self.timer.start()
                self.timer.is_running = True

        return StepResult.DONE


@parameters(
    [
        Property.Number(label="Temp", configurable=True),
        Property.Sensor(label="Sensor"),
        Property.Kettle(label="Kettle"),
        Property.Text(
            label="Notification",
            configurable=True,
            description="Text for notification when Temp is reached",
        ),
        Property.Select(
            label="AutoMode",
            options=["Yes", "No"],
            # Declared, so a step made in an editor switches its kettle on.
            # Missing reads as No, and a Mash In left that way heats nothing:
            # the brew stalls (DEFECTS M41, Gemini's R2 on 2 Nov).
            default_value="Yes",
            description="Switch Kettlelogic automatically on and off -> Yes",
        ),
        Property.Number(
            label="Grain_Kg",
            configurable=True,
            description="Grain bill in kg. With a grain temperature and mash water volume, the step heats past Temp so the mash lands on it after doughing in. Leave blank to heat to Temp as before.",
        ),
        Property.Sensor(
            label="Grain_Sensor",
            description="Ambient sensor. Grain sits at room temperature, so this is its temperature. Required for strike calculation.",
        ),
        Property.Number(
            label="Mash_Water",
            configurable=True,
            description="Mash water in litres. Required for strike calculation.",
        ),
        Property.Select(
            label="Stop_Pump_At_Strike",
            options=["Yes", "No"],
            # What the step does when it is missing; declared so an editor shows it.
            default_value="Yes",
            description="Stop the recirculation pump once strike temperature is reached, while you dough in. Grain should go into a still tun rather than be drawn into a HERMS coil that has no grain bed in front of it. It also bounds the overshoot from a fast mash-in ramp: the coil is the only path from the HLT to the mash, so stopping the pump cuts that path at once, where a heating element would coast. Measured on a 30 L rig driving the HLT flat out - overshoot +3.0 F with the pump running, -0.1 F with it stopped. The pump restarts when you press Next. Set to No if you would rather keep circulating and hold temperature during dough-in.",
        ),
    ]
)
class MashInStep(CBPiStep):

    #: Most a strike temperature may exceed the rest temperature by. A mistyped
    #: grain bill would otherwise drive the water far past the target before
    #: anyone sees it, and the enzymes are what pay for that. In Celsius; scaled
    #: for Fahrenheit at use.
    MAX_STRIKE_RISE_C = 15.0

    #: And an absolute ceiling, safely below anything that would scald the first
    #: grain to touch the water.
    MAX_STRIKE_ABS_C = 82.0

    #: Kept with the step once the water reaches strike and it waits for the
    #: malt, like elapsed time, so Stop and Start - or a restart - come back to
    #: that wait: pump held, no second heat to strike (DEFECTS M35).
    WAITING_FOR_MALT_PROP = "_waiting_for_malt"

    async def NextStep(self, **kwargs):
        step = self.cbpi.step.find_by_id(self.id)
        if (step is None or step.instance is not self
                or step.status != StepState.ACTIVE or not self.running):
            logging.info("Ignoring an old malt acknowledgement for %s", self.id)
            return
        await self.next()

    async def next(self):
        # The brewer confirming the malt is in starts the mash pH reminder, if a
        # plugin such as BrewScribe asked for it (StepController.
        # enable_mash_ph_reminder). Nothing about it may hold the mash up.
        confirmed = getattr(getattr(self.cbpi, "step", None), "_mash_in_confirmed", None)
        if confirmed is not None:
            try:
                await confirmed(self)
            except Exception as error:  # noqa: BLE001
                logging.warning("Could not save the mash pH reminder: %s", error)
                try:
                    self.cbpi.notify(
                        "Mash pH reminder",
                        "Could not save the reminder, so a restart would lose it. If "
                        "it has not appeared 15 minutes after mash-in, check the mash "
                        "pH anyway.",
                        NotificationType.WARNING,
                    )
                except Exception:  # noqa: BLE001
                    pass
        await super().next()

    async def on_timer_done(self, timer):
        # Deliberately does NOT clear the setpoint or switch AutoMode off.
        #
        # This fires the moment the water reaches strike temperature, and then
        # the step waits - indefinitely, and correctly - for the brewer to dough
        # in and press Next. Releasing the kettle here meant nothing held
        # temperature during exactly the minutes a sack of room-temperature grain
        # is going in, which is the largest heat sink of the brew day. The mash
        # then lands below target and, on a HERMS, the correction has to come
        # back through the coil slowly with the grain already in.
        #
        # What it does do is drop the setpoint from strike to rest. The strike
        # temperature is a pre-heat: it exists to be given up to the grain, and
        # once the water is there the job is done. Leaving the setpoint high
        # would have the controller fight the dough-in drop it deliberately
        # planned for, and overshoot the rest temperature afterwards.
        #
        # on_stop still switches AutoMode off. That is where the step is
        # genuinely finished, and it is the right place for it.
        if self.kettle is not None:
            self.kettle.target_temp = getattr(self, "rest_temp", None) or self._float_prop(
                "Temp", 0
            )
        # Strike temperature is reached, so the brewer is about to dough in.
        # From here the tun must be assumed to contain grain - the flag goes
        # back before the first handful goes in, not after the step ends.
        self._set_grain_present(True)
        # And stop the recirculation, for two reasons at once. Grain should go
        # into a still tun rather than be drawn into a coil with no grain bed
        # in front of it; and because the coil is the only heat path on a
        # HERMS, stopping it is what bounds the overshoot from a fast ramp.
        if self.props.get("Stop_Pump_At_Strike", "Yes") != "No":
            self._set_pump_hold(True)
        # Remembered, so a Stop and Start does not forget that the malt may be
        # going in: it restarted the pump into the grain and heated to strike
        # again (DEFECTS M35, found by both reviewers on 2 Nov).
        self._waiting_for_malt = True
        await self.record(self.WAITING_FOR_MALT_PROP, True)
        self.summary = ""
        await self.push_update()
        self._ask_for_malt()

    def _ask_for_malt(self):
        self.cbpi.notify(
            self.name,
            self.props.get(
                "Notification",
                "Target Temp reached. Please add malt and click next to move on.",
            ),
            action=[NotificationAction("Next Step", self.NextStep)],
        )

    async def on_timer_update(self, timer, seconds):
        await self.push_update()

    async def on_start(self):
        self.AutoMode = True if self.props.get("AutoMode", "No") == "Yes" else False
        self.kettle = self.get_kettle(self.props.get("Kettle", None))
        self.rest_temp = self._float_prop("Temp", 0)
        self._waiting_for_malt = bool(self.props.get(self.WAITING_FOR_MALT_PROP, False))
        if self._waiting_for_malt:
            await self._resume_waiting_for_malt()
            return
        self.strike_temp = self._strike_target(self.rest_temp)
        if self.kettle is not None:
            self.kettle.target_temp = self.strike_temp
        # Tell the kettle logic the tun holds only water.
        #
        # A HERMS band is a compromise between ramp rate and not cooking
        # enzymes in the coil, and until the grain goes in there are no enzymes
        # to cook - so that compromise is being paid for nothing, and it is
        # paid almost entirely on the final few degrees where the outer loop
        # eases the HLT back down. Measured on a 30 L / 6.5 kW rig: 13.6 of the
        # 64 minutes to reach a 163 F strike went on the last 5 F.
        #
        # Only a logic that understands the flag does anything with it, and the
        # flag says what is true rather than what to do, so a logic that has
        # never heard of it is unaffected.
        self._set_grain_present(False)
        if self.AutoMode == True:
            await self.setAutoMode(True)
            # Again, now that the logic is on. Switching it on builds a new
            # instance (BasicController.start), so the flag set above landed
            # on the old one, or on none - and the new one started believing
            # the tun held grain. Found 27 Oct: MashIn_Pump_Within did nothing
            # on the sim, and MashIn_DeltaTemp had never widened a mash-in band
            # in a profile since it was added. The first call stays for a
            # logic that was already running, which setAutoMode leaves alone.
            self._set_grain_present(False)
        self.summary = "Waiting for Target Temp"
        if self.cbpi.kettle is not None and self.timer is None:
            self.timer = Timer(
                1, on_update=self.on_timer_update, on_done=self.on_timer_done
            )
        await self.push_update()

    async def _resume_waiting_for_malt(self):
        """Back to the wait for the malt, as the step was when it stopped (M35).

        The water had reached strike: the target is the rest temperature, the
        tun is taken to hold grain, and the pump is held if the step holds it at
        strike - handed to the kettle logic before it runs, because a logic
        that has been stopped starts as a new instance, and one told afterwards
        may already have switched the pump on. Then the prompt again: a restart,
        or another page, may not have it.
        """
        self.strike_temp = self.rest_temp
        if self.kettle is not None:
            self.kettle.target_temp = self.rest_temp
        hold = self.props.get("Stop_Pump_At_Strike", "Yes") != "No"
        flags = {"grain_present": True}
        if hold:
            flags["pump_hold"] = True
        # On a logic still running, now; on the one started below, from the
        # moment it exists.
        self._set_grain_present(True)
        if hold:
            self._set_pump_hold(True)
        if self.AutoMode == True:
            await self.setAutoMode(True, preset=flags)
        self.summary = ""
        if self.cbpi.kettle is not None and self.timer is None:
            self.timer = Timer(
                1, on_update=self.on_timer_update, on_done=self.on_timer_done
            )
        await self.push_update()
        self._ask_for_malt()

    def _float_prop(self, name, default):
        """A numeric property, without truncating the fraction away.

        Kept as a thin alias so MashInStep reads the same as it did; the real
        implementation is on CBPiStep, where every step can reach it.
        """
        return super()._float_prop(name, default)

    def _set_grain_present(self, present):
        """Tell the kettle logic whether the tun holds grain.

        Stated as a fact about the vessel rather than an instruction to the
        controller, so a logic that has never heard of it is unaffected and a
        logic that has can decide for itself what to do. Never raises: a step
        must not fail because a controller does not implement an optimisation.
        """
        self._tell_logic("grain_present", bool(present))

    def _set_pump_hold(self, hold):
        """Ask the kettle logic for no flow.

        Separate from grain_present because they are different facts: one is
        about what is in the vessel, the other is about what the step needs the
        rig to do right now.
        """
        self._tell_logic("pump_hold", bool(hold))

    def _tell_logic(self, name, value):
        try:
            kettle = getattr(self, "kettle", None)
            instance = getattr(kettle, "instance", None) if kettle else None
            if instance is not None:
                setattr(instance, name, value)
        except Exception as e:  # noqa: BLE001
            logging.warning("Could not set %s: %s", name, e)

    def _strike_target(self, rest_temp):
        """Water temperature to heat to, so the mash lands on rest_temp.

        Doughing in drops the mash by several degrees and no feedback controller
        can prevent it - the heat is gone before the sensor reads it. The grain
        mass is in the recipe and the grain is at room temperature, so the drop
        is predictable and can be pre-empted. Measured on a simulated HERMS: an
        8.7 F drop becomes 1.1 F.

        Falls back to the rest temperature whenever anything needed is missing or
        implausible. That is the important half: a strike temperature computed
        from a mistyped grain bill would scald the enzymes before the grain ever
        went in, so this declines rather than guesses, and is clamped even when
        it does answer.
        """
        try:
            grain_kg = float(self.props.get("Grain_Kg", 0) or 0)
            water_l = float(self.props.get("Mash_Water", 0) or 0)
        except (TypeError, ValueError):
            return rest_temp
        if grain_kg <= 0 or water_l <= 0:
            return rest_temp

        sensor_id = self.props.get("Grain_Sensor", None)
        if not sensor_id:
            return rest_temp
        try:
            grain_temp = self.get_sensor_value(sensor_id).get("value")
        except Exception:
            return rest_temp
        if grain_temp is None:
            return rest_temp

        fahrenheit = self.get_config_value("TEMP_UNIT", "C") != "C"
        scale = 1.8 if fahrenheit else 1.0
        result = strike_temperature(
            rest_temp,
            grain_kg,
            grain_temp,
            water_l,
            max_rise=self.MAX_STRIKE_RISE_C * scale,
            absolute_max=(self.MAX_STRIKE_ABS_C * 1.8 + 32) if fahrenheit
            else self.MAX_STRIKE_ABS_C,
        )
        if not result.ok:
            logging.info(
                "MashIn: heating to the rest temperature, no strike calculation (%s)",
                result.reason,
            )
            return rest_temp

        strike = round(result.temperature, 1)
        self.cbpi.notify(
            self.name,
            "Heating to {}{} so the mash lands on {}{} after {} kg of grain at "
            "{}{} goes in.{}".format(
                strike, "F" if fahrenheit else "C",
                rest_temp, "F" if fahrenheit else "C",
                grain_kg, round(grain_temp, 1), "F" if fahrenheit else "C",
                " Clamped to the safety limit - check the grain bill."
                if result.clamped else "",
            ),
            NotificationType.WARNING if result.clamped else NotificationType.INFO,
        )
        return strike

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        # Hand the rig back exactly as it was found, whether this step finished
        # normally or was aborted. Leaving either flag set would silently give
        # the next rest a mash-in band or a stopped pump.
        self._set_grain_present(True)
        self._set_pump_hold(False)
        if self.AutoMode == True:
            await self.setAutoMode(False)
        await self.push_update()

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
            # Waiting for the malt: strike was reached, and nothing more
            # happens here until the brewer presses Next (M35).
            if getattr(self, "_waiting_for_malt", False):
                continue
            sensor_value = self.get_sensor_value(self.props.get("Sensor", None)).get(
                "value"
            )
            # The strike temperature, which equals the rest temperature whenever
            # there is no grain bill to compensate for.
            target = getattr(self, "strike_temp", None)
            if target is None:
                target = self._float_prop("Temp", 0)
            if self.timer.is_running is not True:
                self.note_heat_progress(sensor_value, target)
            if self.target_reached(sensor_value, target) and self.timer.is_running is not True:
                self.timer.start()
                self.timer.is_running = True
        await self.push_update()
        return StepResult.DONE

    async def reset(self):
        self.timer = Timer(
            1, on_update=self.on_timer_update, on_done=self.on_timer_done
        )

    async def setAutoMode(self, auto_state, preset=None):
        """Switch the kettle logic on or off.

        `preset`: attributes a logic started here has before it runs - see
        KettleController.start. Used to resume a dough-in with the pump held.
        """
        try:
            if (
                self.kettle.instance is None or self.kettle.instance.state == False
            ) and (auto_state is True):
                if preset:
                    await self.cbpi.kettle.start(self.kettle.id, preset=preset)
                    await self.cbpi.kettle.push_udpate()
                else:
                    await self.cbpi.kettle.toggle(self.kettle.id)
            elif (
                self.kettle.instance is not None
                and self.kettle.instance.state == True
            ) and (auto_state is False):
                await self.cbpi.kettle.stop(self.kettle.id)
            await self.push_update()

        except Exception as e:
            logging.error(
                "Failed to switch on KettleLogic {} {}".format(self.kettle.id, e)
            )


@parameters(
    [
        Property.Number(
            label="Timer", description="Time in Minutes", configurable=True
        ),
        Property.Number(label="Temp", configurable=True),
        Property.Sensor(label="Sensor"),
        Property.Kettle(label="Kettle"),
        Property.Select(
            label="AutoMode",
            options=["Yes", "No"],
            # See MashInStep: a step made in an editor switches its kettle on.
            default_value="Yes",
            description="Switch Kettlelogic automatically on and off -> Yes",
        ),
    ]
)
class MashStep(CBPiStep):

    @action("Start Timer", [])
    async def start_timer(self):
        if self.timer.is_running is not True:
            self.cbpi.notify(self.name, "Timer started", NotificationType.INFO)
            self.timer.start()
            self.timer.is_running = True
        else:
            self.cbpi.notify(
                self.name, "Timer is already running", NotificationType.WARNING
            )

    @action("Add 5 Minutes to Timer", [])
    async def add_timer(self):
        if self.timer.is_running == True:
            self.cbpi.notify(self.name, "5 Minutes added", NotificationType.INFO)
            await self.timer.add(300)
        else:
            self.cbpi.notify(
                self.name, "Timer must be running to add time", NotificationType.WARNING
            )

    async def on_timer_done(self, timer):
        self.summary = ""
        # Guarded because get_kettle() returns None for a deleted or unset kettle -
        # every on_start in this file already checks, but the timer callbacks did
        # not. Raising here would skip both the notification and the next() call
        # below, wedging the profile on a step that has actually finished.
        if self.kettle is not None:
            self.kettle.target_temp = 0
        if self.AutoMode == True:
            await self.setAutoMode(False)
        self.cbpi.notify(self.name, "Step finished", NotificationType.SUCCESS)
        # The rest is over, so recorded progress must not survive into a later run
        # of the same step.
        self.clear_progress()

        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.note_progress(seconds, self._int_prop("Timer", 0) * 60)
        await self.push_update()

    async def on_start(self):
        self.AutoMode = True if self.props.get("AutoMode", "No") == "Yes" else False
        self.kettle = self.get_kettle(self.props.Kettle)
        if self.kettle is not None:
            self.kettle.target_temp = self._float_prop("Temp", 0)
        if self.AutoMode == True:
            await self.setAutoMode(True)
        await self.push_update()

        if self.cbpi.kettle is not None and self.timer is None:
            self.timer = Timer(
                self.remaining_for(self._int_prop("Timer", 0) * 60),
                on_update=self.on_timer_update,
                on_done=self.on_timer_done,
            )
        elif self.cbpi.kettle is not None:
            try:
                if self.timer.is_running == True:
                    self.timer.start()
            except:
                pass

        self.summary = "Waiting for Target Temp"
        await self.push_update()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        if self.AutoMode == True:
            await self.setAutoMode(False)
        await self.push_update()

    async def reset(self):
        self.timer = Timer(
            self._int_prop("Timer", 0) * 60,
            on_update=self.on_timer_update,
            on_done=self.on_timer_done,
        )

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
            sensor_value = self.get_sensor_value(self.props.get("Sensor", None)).get(
                "value"
            )
            if self.timer.is_running is not True:
                self.note_heat_progress(sensor_value, self.props.get("Temp", 0))
            if (
                self.target_reached(sensor_value, self._float_prop("Temp", 0))
                and self.timer.is_running is not True
            ):
                self.timer.start()
                self.timer.is_running = True
                self.cbpi.notify(
                    self.name,
                    "Timer started. Estimated completion: {}".format(
                        _completion_time(self.timer)
                    ),
                    NotificationType.INFO,
                )
        return StepResult.DONE

    async def setAutoMode(self, auto_state):
        try:
            if (
                self.kettle.instance is None or self.kettle.instance.state == False
            ) and (auto_state is True):
                await self.cbpi.kettle.toggle(self.kettle.id)
            elif (
                self.kettle.instance is not None
                and self.kettle.instance.state == True
            ) and (auto_state is False):
                await self.cbpi.kettle.stop(self.kettle.id)
            await self.push_update()

        except Exception as e:
            logging.error(
                "Failed to switch on KettleLogic {} {}".format(self.kettle.id, e)
            )


@parameters(
    [Property.Number(label="Timer", description="Time in Minutes", configurable=True)]
)
class WaitStep(CBPiStep):

    async def on_timer_done(self, timer):
        self.summary = ""
        # Over: its time must not carry into a later run of it.
        self.clear_progress()
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        # Kept, like a mash rest's, so a restart carries on with what is left
        # (DEFECTS M43, review item 14: a whirlpool stand ran its full 20
        # minutes again after a restart).
        await self.note_progress(seconds, self._int_prop("Timer", 0) * 60)
        await self.push_update()

    async def on_start(self):
        if self.timer is None:
            self.timer = Timer(
                self.remaining_for(self._int_prop("Timer", 0) * 60),
                on_update=self.on_timer_update,
                on_done=self.on_timer_done,
            )
        self.timer.start()

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        await self.push_update()

    async def reset(self):
        self.timer = Timer(
            self._int_prop("Timer", 0) * 60,
            on_update=self.on_timer_update,
            on_done=self.on_timer_done,
        )

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE


def _fmt_temp(value):
    """170.0 -> "170", 170.5 -> "170.5"."""
    return ("%.1f" % value).rstrip("0").rstrip(".")


@parameters(
    [
        Property.Number(
            label="Timer", description="Minutes the stand lasts", configurable=True
        ),
        Property.Number(
            label="Temp",
            configurable=True,
            description="The whirlpool temperature. If the wort falls more than "
            "2 C / 3.6 F below it during the stand, you are asked whether to "
            "heat it back and hold it there until the stand ends. Blank: never "
            "asked.",
        ),
        Property.Kettle(
            label="Kettle",
            description="The kettle the wort stands in - the boil kettle.",
        ),
    ]
)
class WhirlpoolStep(WaitStep):
    """A whirlpool or hop stand: a timed wait in the boil kettle, kept warm on request.

    Scott, 4 Nov, after a brew day on the simulator: "whirlpool step cooled too
    far. I'd like a prompt to ask if I want to raise temp back to whirlpool
    temp and hold". The stand was a WaitStep - a timer and nothing else - and
    the wort lost about 18 F over its 20 minutes with nothing watching it.

    Now, if the wort falls more than 2 C / 3.6 F below Temp, the brewer is
    asked, once a stand, in a prompt kept until it is answered: heat it back
    and hold it there until the stand ends? Heat and hold sets a hold on the
    kettle's logic - BoilPower's Hold a temperature where the logic has one,
    the kettle's target where it has not - and then switches the logic on: in
    that order, so BoilPower never runs a moment without its hold, which would
    be a boil. When the stand ends, or is stopped or skipped, the logic is
    switched off and then the hold cleared, so the chill that follows starts
    with the element off. Stopped and started again, the stand holds again
    without asking.

    The stand's time runs on while the wort reheats: the stand lasts what the
    recipe says.
    """

    # How far below Temp counts as cooled too far: BoilPower's own band for
    # "meaningfully below" (its BOIL_EXIT_DROP_C), where its hold heats again.
    COOLED_C = 2.0

    # Per stand. Class defaults, so a step built without __init__ has them;
    # only immutable values.
    _asked = False  # the question is on show, or was answered
    _prompt_id = None
    _declined = False  # the brewer said No
    _hold_wanted = False  # the brewer said Heat and hold
    _holding = False  # this step set the hold and switched the logic on
    _held_by_action = False  # through the logic's Hold a temperature

    async def on_start(self):
        await super().on_start()
        if self._hold_wanted and not self._holding:
            # Stopped and started again: the brewer has already chosen.
            await self._start_hold()
        await self._check_wort()

    async def on_timer_update(self, timer, seconds):
        await super().on_timer_update(timer, seconds)
        await self._check_wort()

    async def on_stop(self):
        paused = self.cancel_reason == StepResult.STOP
        await self._end_hold(paused)
        # Not answered by now, the question no longer applies: taken down from
        # every page, and asked again if a paused stand is started again.
        self._retire_prompt()
        self._asked = False
        if not paused:
            # Finished or skipped: a later run of this step starts afresh.
            self._declined = False
            self._hold_wanted = False
        await super().on_stop()

    async def reset(self):
        await super().reset()
        self._retire_prompt()
        self._asked = self._declined = self._hold_wanted = False
        self._holding = self._held_by_action = False

    def _whirlpool_temp(self):
        """Temp as a number, or None: blank, or unreadable."""
        raw = self.props.get("Temp", None)
        if raw in (None, ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _kettle(self):
        kettle_id = self.props.get("Kettle", None)
        if not kettle_id:
            return None
        try:
            return self.get_kettle(kettle_id)
        except Exception:  # noqa: BLE001
            return None

    def _wort_temp(self, kettle):
        """The kettle's probe reading, or None if there is no usable one."""
        sensor = getattr(kettle, "sensor", None)
        if not sensor:
            return None
        try:
            reading = self.get_sensor_value(sensor)
            value = reading.get("value") if isinstance(reading, dict) else reading
        except Exception:  # noqa: BLE001
            return None
        return float(value) if self.reading_usable(value) else None

    def _unit(self):
        try:
            unit = str(self.get_config_value("TEMP_UNIT", "C")).upper()
        except Exception:  # noqa: BLE001
            unit = "C"
        return "F" if unit.startswith("F") else "C"

    async def _check_wort(self):
        """Ask, once a stand, if the wort has cooled too far."""
        if self._asked or self._declined or self._hold_wanted or not self.running:
            return
        target = self._whirlpool_temp()
        kettle = self._kettle()
        if target is None or kettle is None:
            return
        value = self._wort_temp(kettle)
        band = self.COOLED_C * (1.8 if self._unit() == "F" else 1.0)
        if value is None or value >= target - band:
            return
        self._ask(value, target, kettle)

    def _ask(self, value, target, kettle):
        self._asked = True
        unit = "\u00b0" + self._unit()
        try:
            minutes = int(round(self.timer.seconds_left() / 60.0))
            left = ", with {} minute{} of the stand left".format(
                minutes, "" if minutes == 1 else "s")
        except Exception:  # noqa: BLE001
            left = ""
        message = (
            "The wort in {kettle} has cooled to {value:.1f} {u}, below the "
            "{target} {u} whirlpool{left}. Heat it back to {target} {u} and hold "
            "it there until the stand ends?".format(
                kettle=getattr(kettle, "name", None) or "the kettle", value=value,
                u=unit, target=_fmt_temp(target), left=left,
            )
        )
        logging.warning("%s: %s", self.name, message)
        try:
            self._prompt_id = _notify_kept(
                self.cbpi, self.name, message, NotificationType.WARNING,
                [NotificationAction("Heat and hold", self._accept),
                 NotificationAction("No", self._decline)],
            )
        except Exception as e:  # noqa: BLE001
            logging.warning("%s: could not ask about the cooled wort: %s", self.name, e)

    async def _accept(self):
        self._prompt_id = None
        if not self.running:
            # A button from a stand that has ended - pressed on a page that
            # never heard the prompt was taken down. It must not switch a
            # kettle on during the chill.
            logging.info("%s: Heat and hold pressed after the stand ended; ignored", self.name)
            return
        self._hold_wanted = True
        await self._start_hold()

    async def _decline(self):
        self._prompt_id = None
        self._declined = True

    def _has_hold_action(self, kettle):
        """Whether the kettle's logic offers Hold a temperature (BoilPower)."""
        try:
            logic = (self.cbpi.kettle.types.get(kettle.type) or {}).get("class")
            return bool(getattr(getattr(logic, "set_hold_target", None), "action", False))
        except Exception:  # noqa: BLE001
            return False

    async def _start_hold(self):
        """Hold the whirlpool temperature, then switch the kettle's logic on."""
        kettle = self._kettle()
        target = self._whirlpool_temp()
        if kettle is None or target is None:
            return
        try:
            by_action = self._has_hold_action(kettle)
            if by_action:
                # Quiet: the logic's own message as it starts holding says what
                # it does; "switched on, it holds" before that is noise.
                held = await self.cbpi.kettle.call_action(
                    kettle.id, "set_hold_target", {"Hold_Target": target, "Quiet": True})
                if held is False:
                    raise RuntimeError("its logic refused the hold")
            else:
                await self.cbpi.kettle.set_target_temp(kettle.id, target)
            self._held_by_action = by_action
            instance = getattr(kettle, "instance", None)
            if instance is None or not getattr(instance, "running", False):
                await self.cbpi.kettle.start(kettle.id)
            self._holding = True
            self.summary2 = "Holding {} \u00b0{}".format(_fmt_temp(target), self._unit())
            await self.push_update()
        except Exception as e:  # noqa: BLE001
            logging.warning("%s: could not hold %s: %s", self.name, target, e)
            try:
                self.cbpi.notify(
                    self.name,
                    "Could not hold {} at {} \u00b0{}: {}. Set it from the "
                    "dashboard if you still want it.".format(
                        getattr(kettle, "name", "the kettle"), _fmt_temp(target),
                        self._unit(), e),
                    NotificationType.WARNING,
                )
            except Exception:  # noqa: BLE001
                pass

    async def _end_hold(self, paused=False):
        """Undo what Heat and hold did: logic off first, then the hold cleared.

        Then say so, here, where it is known why. BoilPower is asked to clear
        the hold quietly: its own words, for a brewer clearing a hold from the
        dashboard, were "Hold cleared: Boil Kettle boils when it is switched
        on" - which at the end of a stand, as the chill starts, read as if the
        kettle were about to boil (Scott, 4 Nov, brewing test recipe v4).
        """
        if not self._holding:
            return
        self._holding = False
        self.summary2 = None
        kettle = self._kettle()
        if kettle is None:
            return
        switched_off = True
        try:
            await self.cbpi.kettle.stop(kettle.id)
        except Exception as e:  # noqa: BLE001
            logging.warning("%s: could not switch %s off: %s", self.name, kettle.id, e)
            switched_off = False
        if self._held_by_action:
            try:
                await self.cbpi.kettle.call_action(
                    kettle.id, "set_hold_target", {"Hold_Target": "", "Quiet": True})
            except Exception as e:  # noqa: BLE001
                logging.warning("%s: could not clear the hold: %s", self.name, e)
        name = getattr(kettle, "name", None) or "The kettle"
        target = self._whirlpool_temp()
        held = ("{} \u00b0{}".format(_fmt_temp(target), self._unit())
                if target is not None else "the whirlpool temperature")
        if not switched_off:
            message = "Could not switch {} off. Switch it off from the dashboard.".format(name)
            level = NotificationType.WARNING
        elif paused:
            message = ("Stand paused: {} is off. It holds {} again when the "
                       "stand is started again.".format(name, held))
            level = NotificationType.INFO
        else:
            message = "Stand over: {} is off and no longer holding {}.".format(name, held)
            level = NotificationType.INFO
        try:
            self.cbpi.notify(self.name, message, level)
        except Exception as e:  # noqa: BLE001
            logging.debug("%s: could not say the hold ended: %s", self.name, e)

    def _retire_prompt(self):
        prompt, self._prompt_id = self._prompt_id, None
        if prompt is None:
            return
        retire = getattr(getattr(self.cbpi, "notification", None), "retire", None)
        if retire is None:
            return
        try:
            retire(prompt)
        except Exception as e:  # noqa: BLE001
            logging.debug("%s: could not take the question down: %s", self.name, e)


def _notify_kept(cbpi, title, message, type, action):
    """notify(..., keep=True) where CraftBeerPi can keep a prompt for every
    page (DEFECTS M47); plain notify where it cannot. Returns the id, if any."""
    try:
        keeps = "keep" in inspect.signature(cbpi.notify).parameters
    except (TypeError, ValueError):
        keeps = False
    if keeps:
        return cbpi.notify(title, message, type, action=action, keep=True)
    return cbpi.notify(title, message, type, action=action)


@parameters(
    [
        Property.Select(
            label="toggle_type",
            options=["On", "Off"],
            description="Choose if Actor should be switched on or off in this step",
        ),
        Property.Actor(
            label="Actor", description="Actor that should be toggled during this step"
        ),
    ]
)
class ToggleStep(CBPiStep):
    async def on_timer_done(self, timer):
        self.summary = ""
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.push_update()

    async def on_start(self):
        if self.timer is None:
            self.timer = Timer(
                1, on_update=self.on_timer_update, on_done=self.on_timer_done
            )
        self.timer.start()
        self.type = self.props.get("toggle_type", "Off")
        self.Actor = self.props.get("Actor", None)
        if self.Actor is not None and self.type == "On":
            await self.actor_on(self.Actor)
        if self.Actor is not None and self.type == "Off":
            await self.actor_off(self.Actor)

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        await self.push_update()

    async def reset(self):
        self.timer = Timer(
            1, on_update=self.on_timer_update, on_done=self.on_timer_done
        )

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE


@parameters(
    [
        Property.Number(
            label="Timer", description="Time in Minutes", configurable=True
        ),
        Property.Actor(label="Actor"),
    ]
)
class ActorStep(CBPiStep):
    async def on_timer_done(self, timer):
        self.summary = ""
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        await self.push_update()

    async def on_start(self):
        if self.timer is None:
            self.timer = Timer(
                self._int_prop("Timer", 0) * 60,
                on_update=self.on_timer_update,
                on_done=self.on_timer_done,
            )
        self.timer.start()
        await self.actor_on(self.props.Actor)

    async def on_stop(self):
        await self.actor_off(self.props.Actor)
        await self.timer.stop()
        self.summary = ""
        await self.push_update()

    async def reset(self):
        self.timer = Timer(
            self._int_prop("Timer", 0) * 60,
            on_update=self.on_timer_update,
            on_done=self.on_timer_done,
        )

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)
        return StepResult.DONE


@parameters(
    [
        Property.Number(
            label="Timer", description="Time in Minutes", configurable=True
        ),
        Property.Number(
            label="Temp", description="Boil temperature", configurable=True
        ),
        # Property.Number(label="DwellTime", description="Time in minutes (Recommended: 5). If temp is not changing significantly within this time, Timer will start. Defaul:0 disabled", configurable=True),
        Property.Sensor(label="Sensor"),
        Property.Kettle(label="Kettle"),
        Property.Select(
            label="LidAlert",
            options=["Yes", "No"],
            description="Remind you as the wort nears boiling (95 C / 203 F). What "
            "it says is the steps_boil_lid_alert setting: by default, to remove "
            "the lid; with a steam condenser, say to turn it on.",
        ),
        Property.Select(
            label="AutoMode",
            options=["Yes", "No"],
            # See MashInStep: a step made in an editor switches its kettle on.
            default_value="Yes",
            description="Switch Kettlelogic automatically on and off -> Yes",
        ),
        # Property.Select(label="AutoTimer",options=["Yes","No"], description="Start Timer automatically if Temp does not change for 5 Minutes and is above 95C/203F"),
        Property.Select(
            "First_Wort",
            options=["Yes", "No"],
            description="First Wort Hop alert if set to Yes",
        ),
        Property.Text(
            "First_Wort_text",
            configurable=True,
            description="First Wort Hop alert text",
        ),
        Property.Number(
            "Hop_1",
            configurable=True,
            description="First Hop alert (minutes before finish)",
        ),
        Property.Text(
            "Hop_1_text", configurable=True, description="First Hop alert text"
        ),
        Property.Number(
            "Hop_2",
            configurable=True,
            description="Second Hop alert (minutes before finish)",
        ),
        Property.Text(
            "Hop_2_text", configurable=True, description="Second Hop alert text"
        ),
        Property.Number(
            "Hop_3",
            configurable=True,
            description="Third Hop alert (minutes before finish)",
        ),
        Property.Text(
            "Hop_3_text", configurable=True, description="Third Hop alert text"
        ),
        Property.Number(
            "Hop_4",
            configurable=True,
            description="Fourth Hop alert (minutes before finish)",
        ),
        Property.Text(
            "Hop_4_text", configurable=True, description="Fourth Hop alert text"
        ),
        Property.Number(
            "Hop_5",
            configurable=True,
            description="Fifth Hop alert (minutes before finish)",
        ),
        Property.Text(
            "Hop_5_text", configurable=True, description="Fifth Hop alert text"
        ),
        Property.Number(
            "Hop_6",
            configurable=True,
            description="Sixth Hop alert (minutes before finish)",
        ),
        Property.Text(
            "Hop_6_text", configurable=True, description="Sixth Hop alert text"
        ),
    ]
)
class BoilStep(CBPiStep):

    #: What the lid alert says when the steps_boil_lid_alert setting is blank.
    DEFAULT_LID_ALERT = "Please remove lid!"

    #: The reminders already given, kept with the step like elapsed time, so
    #: Stop and Start - or a restart - do not give them again: a paused boil
    #: said "Please add 6.00 oz Willamette" twice (DEFECTS M36, found by both
    #: reviewers on 2 Nov). A hop that fell due while the server was down was
    #: never given, so it still is, late.
    FIRST_WORT_DONE_PROP = "_first_wort_done"
    LID_ALERT_DONE_PROP = "_lid_alert_done"
    HOPS_DONE_PROP = "_hops_done"
    #: The hold before this step asked for the first-wort hops ("Ready to boil?
    #: Add ... for first wort"), so its start does not. A restart during the
    #: hold asks again, in the hold.
    FIRST_WORT_HELD_PROP = "_first_wort_held"

    def _hops_done(self):
        done = self.props.get(self.HOPS_DONE_PROP, None) or []
        try:
            return sorted({int(n) for n in done})
        except (TypeError, ValueError):
            return []

    def first_wort_due(self):
        """The first-wort hops still to ask for this brew, as the reminder names
        them ("1.00 oz Magnum", or "" if the recipe names none) - or None if it
        has none, or they have been asked for.

        Asked for before the wort reaches the boil kettle: as a step named for
        the runoff starts (StepController._first_wort_at_runoff), else in the
        hold before this step (wait_for_brewer), else as it starts (run). Until
        4.7.7.post31 only as it started, which since the hold (post23) was
        after the wort was in the kettle. Scott, 5 Nov: first-wort hops "should
        be correctly supported".
        """
        if self.props.get("First_Wort", "No") != "Yes":
            return None
        if self.props.get(self.FIRST_WORT_DONE_PROP, False):
            return None
        return str(self.props.get("First_Wort_text", "") or "").strip()

    def first_wort_asked(self):
        """Asked for before the boil: not again this brew. The controller saves it."""
        self.props[self.FIRST_WORT_DONE_PROP] = True

    def held_for_brewer(self):
        """The profile now holds before this step: if its question asked for the
        first-wort hops, the boil's start does not ask again."""
        if self.first_wort_due() is not None:
            self.props[self.FIRST_WORT_HELD_PROP] = True

    @action("Start Timer", [])
    async def start_timer(self):
        if self.timer.is_running is not True:
            self.cbpi.notify(self.name, "Timer started", NotificationType.INFO)
            self.timer.start()
            self.timer.is_running = True
        else:
            self.cbpi.notify(
                self.name, "Timer is already running", NotificationType.WARNING
            )

    @action("Add 5 Minutes to Timer", [])
    async def add_timer(self):
        if self.timer.is_running == True:
            self.cbpi.notify(self.name, "5 Minutes added", NotificationType.INFO)
            await self.timer.add(300)
        else:
            self.cbpi.notify(
                self.name, "Timer must be running to add time", NotificationType.WARNING
            )

    async def on_timer_done(self, timer):
        self.summary = ""
        # See MashStep.on_timer_done: a missing kettle must not stop the profile
        # advancing off a boil that has genuinely finished.
        if self.kettle is not None:
            self.kettle.target_temp = 0
        if self.AutoMode == True:
            await self.setAutoMode(False)
        self.cbpi.notify(self.name, "Boiling completed", NotificationType.SUCCESS)
        self.clear_progress()
        await self.next()

    async def on_timer_update(self, timer, seconds):
        self.summary = Timer.format_time(seconds)
        self.remaining_seconds = seconds
        await self.note_progress(seconds, self._int_prop("Timer", 0) * 60)
        await self.push_update()

    @action("Wort transferred - start heating", [])
    async def confirm_transfer(self, **kwargs):
        """Arm the boil kettle once the brewer says the wort is in it.

        See _needs_transfer_confirmation. Nothing heats until this runs.

        Guarded against being run by a notification that is no longer current.
        A CraftBeerPi notification stays clickable after the step that raised it
        has stopped - the callback is held in NotificationController's cache,
        not by the step - so without these checks an old "Wort transferred"
        prompt could set a target and switch AutoMode on for a profile that had
        since been stopped, skipped or reset. That is precisely the thing this
        feature exists to prevent, arriving by a different route.
        """
        if getattr(self, "transfer_confirmed", False):
            return
        if not getattr(self, "running", False):
            logging.info(
                "%s: ignoring a transfer confirmation for a step that is no "
                "longer running", self.name
            )
            return
        self.transfer_confirmed = True
        if self.kettle is not None:
            self.kettle.target_temp = float(self.props.get("Temp", 0))
        if self.AutoMode == True:
            await self.setAutoMode(True)
        self.summary = "Waiting for Target Temp"
        self.cbpi.notify(self.name, "Heating the boil kettle",
                         NotificationType.INFO)
        await self.push_update()

    def _needs_transfer_confirmation(self):
        """Should this step wait to be told the wort is actually in the kettle?

        A mash step advances the moment its timer expires - MashStep.
        on_timer_done ends in an unconditional next() - and this step's on_start
        then energizes the boil element. Between those two things is a sparge, a
        lauter and a transfer, none of which the software knows about and all of
        which take a brewer away from the rig.

        So a mash out finishing while nobody is present hands several kilowatts
        to an empty kettle. Nothing else catches it: a probe in an empty vessel
        reads air temperature, which is a perfectly plausible number, so every
        sensor check passes.

        Defaulting to Yes means an existing profile now stops and asks where it
        used to run straight on. That is deliberate: the cost of the prompt is
        one press, and the cost of getting it wrong is a fire. A rig that
        genuinely runs unattended sets CONFIRM_BEFORE_BOIL to No, which is the
        same decision made explicitly rather than by default.

        Since 4.7.7.post23 the profile itself stops before this step and waits
        for the brewer to start it (wait_for_brewer), so the brewer starting it
        is the confirmation. The question in on_start is now asked only when the
        step starts some other way - carried on by itself after a restart.
        """
        return self.get_config_value("CONFIRM_BEFORE_BOIL", "Yes") != "No"

    def wait_for_brewer(self):
        """Why the profile should stop before this step until the brewer starts
        it, as (message, button) - or None to start it like any other step.

        Read by StepController before it moves on by itself: the boil then waits,
        with nothing active and the dashboard free for the pumps, until the
        brewer presses the button, Start or Next. Scott, 1 Nov: "I think it
        should require a manual advance to boil step instead of auto-advance".
        """
        if not self._needs_transfer_confirmation():
            return None
        hops = self.first_wort_due()
        if hops is None:
            first = "Move the wort to the boil kettle first"
        else:
            first = ("Add {} to the boil kettle for first wort, then move the wort "
                     "into it".format(hops or "the first-wort hops"))
        return (
            "Ready to boil? " + first + " - the dashboard "
            "is yours meanwhile. The boil kettle stays off until you press Start "
            "the boil, or Start on the Brew Steps page. (Set CONFIRM_BEFORE_BOIL to "
            "No to go straight on.)",
            "Start the boil",
        )

    async def on_start(self):

        self.lid_temp = 95 if self.get_config_value("TEMP_UNIT", "C") == "C" else 203
        lid_on = self.props.get("LidAlert", "No") == "Yes"
        # The lid actor follows the setting; the alert is given once a brew.
        self.lid_actor_flag = lid_on
        self.lid_flag = lid_on and not self.props.get(self.LID_ALERT_DONE_PROP, False)
        self.lid_actor = self.get_config_value("steps_boil_actor", None)
        # What the lid alert says. A rig with a steam condenser keeps the lid on
        # for the whole boil - Scott's has a Steam Slayer, 1 Nov - and needs
        # reminding to turn the condenser on instead.
        self.lid_alert_text = (
            str(self.get_config_value("steps_boil_lid_alert", "") or "").strip()
            or self.DEFAULT_LID_ALERT
        )
        self.AutoMode = True if self.props.get("AutoMode", "No") == "Yes" else False
        self.AutoTimer = (
            True if self.cbpi.config.get("BoilAutoTimer", "No") == "Yes" else False
        )
        self.first_wort_hop_flag = bool(self.props.get(self.FIRST_WORT_DONE_PROP, False))
        self.first_wort_hop = self.props.get("First_Wort", "No")
        self.first_wort_hop_text = self.props.get("First_Wort_text", None)
        done = self._hops_done()
        self.hops_added = [True if n in done else "" for n in range(1, 7)]
        self.remaining_seconds = None
        self.temparray = np.array([])
        self.temptimes = np.array([])
        # self.dwelltime=int(self.props.get("DwellTime", 0))*60
        self.dwelltime = (
            5 * 60
        )  # seconds of steady temperature before the boil timer may auto-start
        self.deviationlimit = 0.3  # derived from a test
        # logging.warning(self.AutoTimer)
        self.summary2 = None

        self.kettle = self.get_kettle(self.props.get("Kettle", None))
        # Started by the brewer - from a hold before the boil, or Start, or
        # Resume - is the confirmation: they know where the wort is.
        self.transfer_confirmed = (
            not self._needs_transfer_confirmation()
            or bool(getattr(self, "started_by_brewer", False))
        )
        if self.kettle is not None:
            # Left at zero until the transfer is confirmed, so nothing heats
            # even if a kettle logic is started by hand in the meantime.
            self.kettle.target_temp = (
                float(self.props.get("Temp", 0)) if self.transfer_confirmed else 0
            )

        if self.cbpi.kettle is not None and self.timer is None:
            self.timer = Timer(
                self.remaining_for(self._int_prop("Timer", 0) * 60),
                on_update=self.on_timer_update,
                on_done=self.on_timer_done,
            )

        elif self.cbpi.kettle is not None:
            try:
                if self.timer.is_running == True:
                    self.timer.start()
            except:
                pass

        if self.transfer_confirmed:
            self.summary = "Waiting for Target Temp"
            if self.AutoMode == True:
                await self.setAutoMode(True)
        else:
            # Stop here and ask. The action is what arms the element; until it
            # is pressed this step heats nothing and its timer cannot start.
            self.summary = "Waiting for wort transfer"
            self.cbpi.notify(
                self.name,
                "Is the wort in the boil kettle? Nothing will be heated until "
                "you confirm. (Set CONFIRM_BEFORE_BOIL to No to skip this.)",
                NotificationType.WARNING,
                action=[NotificationAction("Wort transferred",
                                           self.confirm_transfer)],
            )
        await self.push_update()

    async def next_hop_timer(self):
        hop_timers = []
        # Hop_1..Hop_6 are declared as properties and hops_added is sized for six,
        # so the range must reach 7 or the sixth addition is never counted.
        for x in range(1, 7):
            try:
                hop = int(self.props.get("Hop_%s" % x, None)) * 60
            except:
                hop = None
            if hop is not None and self.remaining_seconds is not None:
                hop_left = self.remaining_seconds - hop
                if hop_left > 0:
                    hop_timers.append(hop_left)
        if hop_timers:
            next_hop_timer = time.strftime("%H:%M:%S", time.gmtime(min(hop_timers)))
        else:
            next_hop_timer = None
        return next_hop_timer

    async def check_hop_timer(self, number, value, text):
        if value is not None and self.hops_added[number - 1] is not True:
            if self.remaining_seconds != None and self.remaining_seconds <= (
                int(value) * 60 + 1
            ):
                self.hops_added[number - 1] = True
                await self.record(self.HOPS_DONE_PROP,
                                  sorted(set(self._hops_done()) | {number}))
                # An acknowledge action is what makes this stick. The UI arms a
                # five second auto-dismiss only when the action list is empty
                # (AlertProvider: `if (action?.length <= 0)`), and renders a
                # notification that has actions as a dialog that waits. Hands in
                # grain, or ten feet away with a pump running, a five second
                # toast for a 60/20/10/5/0 schedule is simply missed - and a
                # missed hop cannot be added late.
                if text is not None and text != "":
                    self.cbpi.notify(
                        "Hop Alert",
                        "Please add %s (%s)" % (text, number),
                        NotificationType.WARNING,
                        action=[NotificationAction("Added")],
                    )
                else:
                    self.cbpi.notify(
                        "Hop Alert",
                        "Please add Hop %s" % number,
                        NotificationType.WARNING,
                        action=[NotificationAction("Added")],
                    )

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        self.summary2 = None
        # Stopping must always succeed. Raising here would leave the step running
        # with its heater still under AutoMode, which is the opposite of what
        # stopping is for.
        if self.kettle is not None:
            self.kettle.target_temp = 0
        if self.AutoMode == True:
            await self.setAutoMode(False)
        if self.lid_actor is not None:
            try:
                await self.actor_off(self.lid_actor) 
            except:
                pass
        # Disarm. A notification stays clickable after the step that raised it
        # has stopped, so leaving this set would let an old prompt re-arm a
        # stopped step - and a boil restarted by anything but the brewer must
        # ask again, because whatever stopped it may well have been the brewer
        # noticing the kettle was empty. (Restarted by the brewer - Start,
        # Resume - it does not ask: see on_start.)
        self.transfer_confirmed = False
        await self.push_update()

    async def reset(self):
        self.transfer_confirmed = False
        self.timer = Timer(
            self._int_prop("Timer", 0) * 60,
            on_update=self.on_timer_update,
            on_done=self.on_timer_done,
        )

    async def run(self):
        if self.first_wort_hop_flag == False and self.first_wort_hop == "Yes":
            self.first_wort_hop_flag = True
            await self.record(self.FIRST_WORT_DONE_PROP, True)
            # Sticky, like the other hop alerts: with an action the interface
            # holds it until answered instead of dismissing it after five
            # seconds - and it is raised alongside the wort-transfer prompt,
            # exactly when hands are busy. Not when the hold before the boil
            # asked for them: starting the boil, the wort is in the kettle.
            if self.props.get(self.FIRST_WORT_HELD_PROP, False):
                pass
            elif self.first_wort_hop_text is not None and self.first_wort_hop_text != "":
                self.cbpi.notify(
                    "First Wort Hop Addition!",
                    "Please add %s for first wort" % self.first_wort_hop_text,
                    NotificationType.WARNING,
                    action=[NotificationAction("Added")],
                )
            else:
                self.cbpi.notify(
                    "First Wort Hop Addition!",
                    "Please add hops for first wort",
                    NotificationType.WARNING,
                    action=[NotificationAction("Added")],
                )

        while self.running == True:
            # Brewing time, because this loop measures with its own cadence: the
            # dwell window below is built from these samples, so sampling on
            # wall time while the window is held in brewing time would make the
            # two disagree by exactly the time scale. Identical to
            # asyncio.sleep(1) on a real rig, where the two clocks are one.
            await clock.sleep(1)

            # Nothing happens until the brewer says the wort is in the kettle.
            # Held here as well as in on_start because this loop is what starts
            # the boil timer: a kettle left hot from a previous step would
            # otherwise satisfy the target check below and begin timing a boil
            # that is not happening.
            if not getattr(self, "transfer_confirmed", True):
                continue

            sensor_value = self.get_sensor_value(self.props.get("Sensor", None)).get(
                "value"
            )
            self.temparray = np.append(self.temparray, sensor_value)
            self.temptimes = np.append(self.temptimes, clock.now())

            # Keep the last dwelltime SECONDS, not the last dwelltime samples.
            #
            # This counted array entries and called them seconds, on the
            # assumption that the loop ticks once a second. It does not
            # reliably: the original comment here conceded "not exactly 5 min
            # due to accuracy of asyncio.sleep", and under a scaled clock the
            # gap is not small - a hundredth of the intended window, or a
            # hundred times it, depending on which way the scale runs. Holding
            # the window in time says what was meant and is right at any rate.
            window_start = clock.now() - self.dwelltime
            keep = self.temptimes >= window_start
            self.temparray = self.temparray[keep]
            self.temptimes = self.temptimes[keep]

            have_full_window = (
                self.temptimes.size > 1
                and (self.temptimes[-1] - self.temptimes[0]) >= self.dwelltime - 1
            )
            if have_full_window:
                deviation = np.std(self.temparray)
                if (
                    (sensor_value >= self.lid_temp)
                    and (deviation <= self.deviationlimit)
                    and (self.AutoTimer is True)
                    and (self.timer.is_running is not True)
                ):
                    self.timer.start()
                    self.timer.is_running = True
                    self.cbpi.notify(
                        self.name,
                        "Timer started automatically. Estimated completion: {}".format(
                            _completion_time(self.timer)
                        ),
                        NotificationType.INFO,
                    )
                logging.info(
                    "Current: " + str(sensor_value) + " | Dev: " + str(deviation)
                )
            if self.lid_flag == True and sensor_value >= self.lid_temp:
                # Sticky for the same reason as the hop alert: this one is the
                # difference between lifting the lid and mopping a boilover off
                # the floor, and it fires exactly once. Its words are a setting
                # (steps_boil_lid_alert), so its button says only that it is done.
                self.cbpi.notify(
                    self.lid_alert_text,
                    "Reached temp close to boiling",
                    NotificationType.WARNING,
                    action=[NotificationAction("Done")],
                )
                self.lid_flag = False
                await self.record(self.LID_ALERT_DONE_PROP, True)

            if self.lid_actor is not None and sensor_value >= self.lid_temp and self.lid_actor_flag:
                if self.get_actor_state(self.lid_actor) is False:
                    await self.actor_on(self.lid_actor)

            if self.timer.is_running is not True:
                self.note_heat_progress(sensor_value, self.props.get("Temp", 0))

            if (
                self.target_reached(sensor_value, float(self.props.get("Temp", 0)))
                and self.timer.is_running is not True
            ):
                self.timer.start()
                self.timer.is_running = True
                self.cbpi.notify(
                    self.name,
                    "Timer started. Estimated completion: {}".format(
                        _completion_time(self.timer)
                    ),
                    NotificationType.INFO,
                )
            else:
                if self.timer.is_running:
                    try:
                        nexthoptimer = await self.next_hop_timer()
                        self.summary2 = (
                            "Add Hop in: %s" % nexthoptimer
                            if nexthoptimer is not None
                            else None
                        )
                    except:
                        self.summary2 = None
                for x in range(1, 7):
                    await self.check_hop_timer(
                        x,
                        self.props.get("Hop_%s" % x, None),
                        self.props.get("Hop_%s_text" % x, None),
                    )

        return StepResult.DONE

    async def setAutoMode(self, auto_state):
        try:
            if (
                self.kettle.instance is None or self.kettle.instance.state == False
            ) and (auto_state is True):
                await self.cbpi.kettle.toggle(self.kettle.id)
            elif (
                self.kettle.instance is not None
                and self.kettle.instance.state == True
            ) and (auto_state is False):
                await self.cbpi.kettle.stop(self.kettle.id)
            await self.push_update()

        except Exception as e:
            logging.error(
                "Failed to switch on KettleLogic {} {}".format(self.kettle.id, e)
            )


@parameters(
    [
        Property.Number(
            label="Temp",
            configurable=True,
            description="Target temperature for cooldown. Notification will be send when temp is reached and Actor can be triggered",
        ),
        Property.Sensor(
            label="Sensor", description="Sensor that is used during cooldown"
        ),
        Property.Actor(
            label="Actor",
            description="Actor can trigger a valve for the cooldwon to target temperature",
        ),
        Property.Kettle(label="Kettle"),
    ]
)
class CooldownStep(CBPiStep):

    async def on_timer_done(self, timer):
        self.summary = ""
        if self.actor is not None:
            await self.actor_off(self.actor)
        self.cbpi.notify(
            "CoolDown",
            "Wort cooled down. Please transfer to Fermenter.",
            NotificationType.INFO,
        )
        await self.next()

    async def on_timer_update(self, timer, seconds):
        await self.push_update()

    async def on_start(self):
        try:
            warnings.simplefilter("ignore", np.exceptions.RankWarning)
        except Exception as e:
            logging.error(f"Numpy Error: {e}")
        self.temp_array = []
        self.time_array = []
        self.kettle = self.get_kettle(self.props.get("Kettle", None))
        self.actor = self.props.get("Actor", None)
        self.target_temp = self._float_prop("Temp", 0)
        self.Interval = (
            10  # Interval in minutes on how often cooldwon end time is calculated
        )

        self.cbpi.notify(
            self.name,
            "Cool down to {}°".format(self.target_temp),
            NotificationType.INFO,
        )
        if self.timer is None:
            self.timer = Timer(
                1, on_update=self.on_timer_update, on_done=self.on_timer_done
            )
        self.start_time = time.time()
        self.temp_array.append(
            self.get_sensor_value(self.props.get("Sensor", None)).get("value")
        )
        self.time_array.append(time.time())
        self.next_check = self.start_time + self.Interval * 60
        self.count = 0
        self.initial_date = None

    async def on_stop(self):
        await self.timer.stop()
        self.summary = ""
        if self.actor is not None:
            await self.actor_off(self.actor)
        await self.push_update()

    async def reset(self):
        self.timer = Timer(
            1, on_update=self.on_timer_update, on_done=self.on_timer_done
        )

    async def run(self):
        timestring = datetime.fromtimestamp(self.start_time)
        if self.actor is not None:
            await self.actor_on(self.actor)
        self.summary = "Started: {}".format(timestring.strftime("%H:%M"))
        await self.push_update()
        while self.running == True:
            current_temp = self.get_sensor_value(self.props.get("Sensor", None)).get(
                "value"
            )
            if self.count == 3:
                self.temp_array.append(current_temp)
                current_time = time.time()
                if self.initial_date == None:
                    self.initial_date = current_time
                self.time_array.append(current_time)
                self.count = 0
            if time.time() >= self.next_check:
                self.next_check = time.time() + (self.Interval * 60)

                cooldown_model = np.polynomial.polynomial.Polynomial.fit(
                    self.temp_array, self.time_array, 2
                )
                target_time = cooldown_model(self.target_temp)
                target_timestring = datetime.fromtimestamp(target_time)
                self.summary = "ECT: {}".format(target_timestring.strftime("%H:%M"))
                self.cbpi.notify(
                    "Cooldown Step",
                    "Current: {}°, reaching {}° at {}".format(
                        round(current_temp, 1),
                        self.target_temp,
                        target_timestring.strftime("%d.%m %H:%M"),
                    ),
                    NotificationType.INFO,
                )
                self.temp_array = []
                self.time_array = []
                self.temp_array.append(
                    self.get_sensor_value(self.props.get("Sensor", None)).get("value")
                )
                self.time_array.append(time.time())
                await self.push_update()

            if current_temp <= self.target_temp and self.timer.is_running is not True:
                self.timer.start()
                self.timer.is_running = True

            self.count += 1
            await asyncio.sleep(1)

        return StepResult.DONE


def setup(cbpi):
    """
    This method is called by the server during startup
    Here you need to register your plugins at the server

    :param cbpi: the cbpi core
    :return:
    """

    cbpi.plugin.register("MashInStep", MashInStep)
    cbpi.plugin.register("MashStep", MashStep)
    cbpi.plugin.register("BoilStep", BoilStep)
    cbpi.plugin.register("CooldownStep", CooldownStep)
    cbpi.plugin.register("WaitStep", WaitStep)
    cbpi.plugin.register("WhirlpoolStep", WhirlpoolStep)
    cbpi.plugin.register("ToggleStep", ToggleStep)
    cbpi.plugin.register("ActorStep", ActorStep)
    cbpi.plugin.register("NotificationStep", NotificationStep)
