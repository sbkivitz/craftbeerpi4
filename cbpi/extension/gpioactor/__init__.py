import asyncio
import logging
from unittest.mock import MagicMock, patch

from cbpi.api import *

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except Exception:
    logger.warning("Failed to load RPi.GPIO. Using Mock instead")
    MockRPi = MagicMock()
    modules = {"RPi": MockRPi, "RPi.GPIO": MockRPi.GPIO}
    patcher = patch.dict("sys.modules", modules)
    patcher.start()
    import RPi.GPIO as GPIO

mode = GPIO.getmode()
if mode is None:
    GPIO.setmode(GPIO.BCM)


def _clamp_duty(value):
    """A duty percentage a GPIO actor can actually use. Never raises.

    `power` was stored exactly as handed over, and the pulse loop then computed
    its on and off phases from it. A NaN made both `heating_time > 0` and
    `wait_time > 0` false, so neither branch slept and `run()` spun without
    awaiting anything - which starves the event loop. Nothing else could run:
    not the OFF this actor had been sent, not cancellation, not shutdown. The
    pin stayed at whatever level it was last driven to, with several kilowatts
    behind it.

    An unusable value fails to 0 rather than 100, for the same reason
    ActorController._clamp_power does: when the number is meaningless, the only
    defensible output is no heat.

    Module level because both actors in this file need it. GPIOPWMActor had no
    validation at all and handed `power` straight through to RPi.GPIO.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        return 0
    return max(0, min(100, numeric))


@parameters(
    [
        Property.Select(
            label="GPIO",
            options=[
                0,
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
            ],
        ),
        Property.Select(
            label="Inverted",
            options=["Yes", "No"],
            description="No: Active on high; Yes: Active on low",
        ),
        Property.Select(
            label="SamplingTime",
            options=[2, 5],
            description="Time in seconds for power base interval (Default:5)",
        ),
    ]
)
class GPIOActor(CBPiActor):

    # Custom property which can be configured by the user
    @action(
        "Set Power",
        parameters=[
            Property.Number(
                label="Power", configurable=True, description="Power Setting [0-100]"
            )
        ],
    )
    async def setpower(self, Power=None, **kwargs):
        # Defaulting to 100 meant an action invoked with no value - an empty
        # payload from the interface or an integration - drove the element to
        # full power. ActorController.on() already refuses that inference for
        # the same reason: treating "unset" as a level is how a deliberate 0
        # became 100 there.
        #
        # int(Power) also raised on a NaN or an empty string, so a malformed
        # request failed inside the action rather than being rejected.
        if Power is None:
            Power = self.power if self.power is not None else 0
        self.power = _clamp_duty(Power)
        await self.set_power(self.power)

    def get_GPIO_state(self, state):
        # ON
        if state == 1:
            return 1 if self.inverted == False else 0
        # OFF
        if state == 0:
            return 0 if self.inverted == False else 1

    # Bounds on the time-proportioning period, in seconds.
    #
    # An SSR switches at zero-crossing with no moving parts, so it can run a
    # far shorter period than the 5s default and deliver something much closer
    # to continuous power - at 5s and 20% duty the element is on for one second
    # and off for four, which a kettle sees as a pulse rather than a simmer.
    #
    # The lower bound is not arbitrary. run() sleeps heating_time and then
    # wait_time; at a period of 0 both are zero, so neither branch sleeps and
    # the loop spins, starving the event loop that carries every PID and the
    # shutdown sweep that de-energizes the elements. A contactor should be
    # nowhere near this bound - it has contacts to burn - which is why the
    # period is configurable rather than simply lowered.
    MIN_SAMPLE_TIME = 0.1
    MAX_SAMPLE_TIME = 60.0

    def _sample_time(self):
        """The switching period, as a float and never zero.

        Read with int(), so anything below 1s truncated to 0 and a sub-second
        period - the whole point of using an SSR - was unreachable. A malformed
        value falls back rather than raising: this runs in on_start, and an
        exception there leaves an actor that cannot be commanded at all.
        """
        try:
            value = float(self.props.get("SamplingTime", 5))
        except (TypeError, ValueError):
            logger.warning(
                "ACTOR %s: SamplingTime=%r is not a number, using 5s",
                self.id, self.props.get("SamplingTime"),
            )
            return 5.0
        if value != value:  # NaN
            return 5.0
        if value < self.MIN_SAMPLE_TIME:
            logger.warning(
                "ACTOR %s: SamplingTime %.3fs is below the %.1fs minimum and "
                "would spin the control loop; using the minimum",
                self.id, value, self.MIN_SAMPLE_TIME,
            )
            return self.MIN_SAMPLE_TIME
        return min(self.MAX_SAMPLE_TIME, value)

    async def on_start(self):
        self.power = None
        self.gpio = self.props.GPIO
        self.inverted = True if self.props.get("Inverted", "No") == "Yes" else False
        self.sampleTime = self._sample_time()
        GPIO.setup(self.gpio, GPIO.OUT)
        GPIO.output(self.gpio, self.get_GPIO_state(0))
        self.state = False

    @classmethod
    def _duty(cls, value):
        """A duty percentage the pulse loop can actually use. Never raises.

        Kept as a classmethod because it is part of this actor's tested
        surface; the implementation moved to module level so GPIOPWMActor can
        use it too. See _clamp_duty.
        """
        return _clamp_duty(value)

    async def on(self, power=None):
        self.power = self._duty(power if power is not None else 100)

        logger.info("ACTOR %s ON - GPIO %s " % (self.id, self.gpio))
        # Only energize if there is duty to deliver.
        #
        # This drove the pin high unconditionally, so on(0) asserted the output
        # and left it there until the pulse loop next came round - up to a
        # second, because an idle loop sleeps for one. A zero-duty start is a
        # legitimate command and it must not deliver an unsolicited pulse.
        if self.power > 0:
            GPIO.output(self.gpio, self.get_GPIO_state(1))
        else:
            GPIO.output(self.gpio, self.get_GPIO_state(0))
        self.state = True

    async def off(self):
        logger.info("ACTOR %s OFF - GPIO %s " % (self.id, self.gpio))
        GPIO.output(self.gpio, self.get_GPIO_state(0))
        self.state = False

    async def on_stop(self):
        """Leave the pin low whatever happened.

        run() drives the GPIO high and then awaits the heating part of the
        duty cycle. Stop the actor - or cancel its task - during that await and
        the loop exits with the output still energized and nothing left running
        to lower it. The element stays on until something else happens to
        touch that pin.

        CBPiActor._run() calls this from a finally, so it also covers
        cancellation. It must therefore never raise: this is the last code that
        gets to turn the heat off.
        """
        try:
            GPIO.output(self.gpio, self.get_GPIO_state(0))
        except Exception as e:  # noqa: BLE001
            logger.error("ACTOR %s failed to lower GPIO %s on stop: %s",
                         self.id, getattr(self, "gpio", "?"), e)
        self.state = False

    def get_state(self):
        return self.state

    async def run(self):
        while self.running == True:
            if self.state == True:
                # Validated every pass, not just when it is set. The value can
                # be written from anywhere between two iterations.
                duty = self._duty(self.power)
                sample = self.sampleTime
                if not isinstance(sample, (int, float)) or sample != sample or sample <= 0:
                    sample = self.MIN_SAMPLE_TIME
                heating_time = sample * (duty / 100)
                wait_time = sample - heating_time
                if heating_time > 0:
                    GPIO.output(self.gpio, self.get_GPIO_state(1))
                    await asyncio.sleep(heating_time)
                if wait_time > 0:
                    GPIO.output(self.gpio, self.get_GPIO_state(0))
                    await asyncio.sleep(wait_time)
                if heating_time <= 0 and wait_time <= 0:
                    # Unreachable with a validated duty and a positive sample
                    # time, and kept anyway: an iteration of this loop that
                    # awaits nothing starves the event loop, and the first
                    # casualty is the OFF that would have stopped the element.
                    # The cost of being wrong here is not a slow loop, it is a
                    # heater that cannot be switched off.
                    GPIO.output(self.gpio, self.get_GPIO_state(0))
                    await asyncio.sleep(self.MIN_SAMPLE_TIME)
            else:
                await asyncio.sleep(1)

    async def set_power(self, power):
        self.power = self._duty(power)
        await self.cbpi.actor.actor_update(self.id, self.power)


@parameters(
    [
        Property.Select(
            label="GPIO",
            options=[
                0,
                1,
                2,
                3,
                4,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
            ],
        ),
        Property.Number(label="Frequency", configurable=True),
        Property.Select(
            label="Inverted",
            options=["Yes", "No"],
            description="Inverts PWM load if set to yes (e.g. 90% = 10%). Default: No",
        ),
    ]
)
class GPIOPWMActor(CBPiActor):

    # Custom property which can be configured by the user
    @action(
        "Set Power",
        parameters=[
            Property.Number(
                label="Power", configurable=True, description="Power Setting [0-100]"
            )
        ],
    )
    async def setpower(self, Power=None, **kwargs):
        logging.info(Power)
        # Defaulting to 100 meant an action invoked with no value - an empty
        # payload from the interface or an integration - drove the element to
        # full power. ActorController.on() already refuses that inference for
        # the same reason: treating "unset" as a level is how a deliberate 0
        # became 100 there.
        #
        # int(Power) also raised on a NaN or an empty string, so a malformed
        # request failed inside the action rather than being rejected.
        if Power is None:
            Power = self.power if self.power is not None else 0
        self.power = _clamp_duty(Power)
        await self.set_power(self.power)

    async def on_start(self):
        self.gpio = self.props.get("GPIO", None)
        self.inverted = self.props.get("Inverted", "No")
        self.frequency = self.props.get("Frequency", 0.5)
        if self.gpio is not None:
            GPIO.setup(self.gpio, GPIO.OUT)
            if self.inverted == "No":
                GPIO.output(self.gpio, 0)
            else:
                GPIO.output(self.gpio, 1)
        self.state = False
        self.power = None
        self.p = None
        pass

    async def on(self, power=None):
        logging.debug("PWM Actor Power: {}".format(power))
        # Validated like GPIOActor's. This handed `power` straight to RPi.GPIO,
        # so a NaN or an out-of-range value reached the PWM driver unchecked.
        self.power = _clamp_duty(power if power is not None else 100)

        logging.debug("PWM Final Power: {}".format(self.power))

        logger.debug(
            "PWM ACTOR %s ON - GPIO %s - Frequency %s - Power %s"
            % (self.id, self.gpio, self.frequency, self.power)
        )
        try:
            if self.p is None:
                self.p = GPIO.PWM(int(self.gpio), float(self.frequency))
            if self.inverted == "No":
                self.p.start(self.power)
            else:
                self.p.start(100 - self.power)
            self.state = True
        except Exception as e:
            # Was `except: pass`. That swallowed CancelledError and every real
            # driver fault without a word, so an element or pump that failed to
            # start looked exactly like one that started. On a HERMS a pump that
            # silently fails to run leaves the coil with no flow while the HLT
            # keeps heating, and nobody is told.
            #
            # Left off and reported. ActorController.on() turns the exception
            # into a logged failure and a False return, which is what the caller
            # needs in order to know.
            self.state = False
            logger.error(
                "PWM ACTOR %s failed to switch on - GPIO %s - %s",
                self.id,
                self.gpio,
                e,
            )
            raise

    async def off(self):
        logger.info("PWM ACTOR %s OFF - GPIO %s " % (self.id, self.gpio))
        # self.p is None before the first successful on(), and again after
        # on_stop() clears it. This called ChangeDutyCycle on it regardless, so
        # switching off an actor that had never started raised AttributeError -
        # an actor that cannot be turned off, during whatever sequence was
        # trying to turn it off.
        if self.p is None:
            # Nothing is driving the pin, so there is nothing to fail at.
            self.state = False
            return

        try:
            if self.inverted == "No":
                self.p.ChangeDutyCycle(0)
            else:
                self.p.ChangeDutyCycle(100)
            self.state = False
            return
        except Exception as e:  # noqa: BLE001
            logger.error(
                "PWM ACTOR %s could not be driven off - GPIO %s - %s",
                self.id,
                self.gpio,
                e,
            )

        # Try harder before giving up. A failed duty write does not mean the
        # channel has stopped, so stop the PWM outright, and failing that drive
        # the pin to its de-energized level directly.
        for attempt in (self._stop_pwm, self._force_pin_low):
            try:
                attempt()
                self.state = False
                logger.warning(
                    "PWM ACTOR %s was de-energized by fallback after a failed "
                    "duty write - GPIO %s",
                    self.id,
                    self.gpio,
                )
                return
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "PWM ACTOR %s fallback de-energize failed - GPIO %s - %s",
                    self.id,
                    self.gpio,
                    e,
                )

        # OFF could not be established. Do NOT report it as established.
        #
        # An earlier version set state = False here, reasoning that a failure
        # to drive the pin should not also leave the actor believing it was
        # running. That is backwards for the interlock, which is the thing
        # standing between two multi-kilowatt elements and a supply that can
        # carry only one. A false OFF releases it: measured, the controller
        # reported OFF, the channel retained 85% duty, and the other heater was
        # then ALLOWED to start. The revision before that reported failure,
        # kept the state ON, and refused it.
        #
        # So an actor whose output cannot be verified off stays on as far as
        # everything else is concerned, and the failure reaches the caller.
        # Known-unsafe must not be made indistinguishable from known-safe.
        raise RuntimeError(
            "PWM ACTOR {} could not be switched off on GPIO {}".format(
                self.id, self.gpio
            )
        )

    def _stop_pwm(self):
        self.p.stop()
        self.p = None

    def _force_pin_low(self):
        GPIO.output(self.gpio, 0 if self.inverted == "No" else 1)

    async def set_power(self, power):
        power = _clamp_duty(power)
        if self.p and self.state == True:
            if self.inverted == "No":
                self.p.ChangeDutyCycle(power)
            else:
                self.p.ChangeDutyCycle(
                    100 - power
                )  # Set power to 100-value to invert output
        await self.cbpi.actor.actor_update(self.id, power)
        pass

    def get_state(self):
        return self.state

    async def run(self):
        while self.running == True:
            await asyncio.sleep(1)

    async def on_stop(self):
        """Stop the PWM and leave the pin low.

        Without this the duty cycle simply stays wherever it was when the task
        ended - a stopped actor that is still delivering power. Stopping the
        PWM channel also releases it, so restarting the actor does not leave an
        orphaned channel driving the same pin.

        Called from a finally in CBPiActor._run(), so it must never raise.
        """
        try:
            if self.p is not None:
                self.p.ChangeDutyCycle(0 if self.inverted == "No" else 100)
                self.p.stop()
                self.p = None
        except Exception as e:  # noqa: BLE001
            logger.error("PWM ACTOR %s failed to stop PWM on %s: %s",
                         self.id, getattr(self, "gpio", "?"), e)
        try:
            if self.gpio is not None:
                GPIO.output(self.gpio, 0 if self.inverted == "No" else 1)
        except Exception as e:  # noqa: BLE001
            logger.error("PWM ACTOR %s failed to lower GPIO %s on stop: %s",
                         self.id, getattr(self, "gpio", "?"), e)
        self.state = False


def setup(cbpi):
    """
    This method is called by the server during startup
    Here you need to register your plugins at the server

    :param cbpi: the cbpi core
    :return:
    """

    cbpi.plugin.register("GPIOActor", GPIOActor)
    cbpi.plugin.register("GPIOPWMActor", GPIOPWMActor)
