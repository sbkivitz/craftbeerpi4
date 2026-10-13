import asyncio
import logging

from cbpi.api import *
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
      - a profile that was running when the server stopped is resumed (load()
        marks its active step STOP), and a profile nobody started is left
        alone.

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
                if self.controller._find_by_status(item.steps, StepState.STOP) is not None:
                    await self.controller.start(item.id)
                    logging.info("Autostart: resumed the profile of %s", item.name)
            except Exception as e:  # noqa: BLE001 - one fermenter must not stop the rest
                logging.error("Autostart failed for fermenter %s: %s",
                              getattr(item, "name", "?"), e)

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
class FermenterHysteresis(CBPiFermenterLogic):

    async def run(self):
        try:
            self.heater_offset_min = float(self.props.get("HeaterOffsetOn", 0.2))
            self.heater_offset_max = float(self.props.get("HeaterOffsetOff", 0))
            self.cooler_offset_min = float(self.props.get("CoolerOffsetOn", 0.2))
            self.cooler_offset_max = float(self.props.get("CoolerOffsetOff", 0))
            self.heater_max_power = int(self.props.get("HeaterMaxPower", 100))
            self.cooler_max_power = int(self.props.get("CoolerMaxPower", 100))
            self.heater_max_output = self.heater_max_power
            self.cooler_max_output = self.cooler_max_power


            self.fermenter = self.get_fermenter(self.id)
            self.heater = self.fermenter.heater
            self.cooler = self.fermenter.cooler

            heater = self.cbpi.actor.find_by_id(self.heater)
            cooler = self.cbpi.actor.find_by_id(self.cooler)

            while self.running == True:

                sensor_value = float(
                    self.get_sensor_value(self.fermenter.sensor).get("value")
                )
                target_temp = float(self.get_fermenter_target_temp(self.id))

                try:
                    heater_state = heater.instance.state
                except:
                    heater_state = False
                try:
                    cooler_state = cooler.instance.state
                except:
                    cooler_state = False

                if sensor_value + self.heater_offset_min <= target_temp:
                    if self.heater and (heater_state == False):
                        await self.actor_on(
                            self.heater, self.heater_max_power, self.heater_max_output
                        )

                if sensor_value + self.heater_offset_max >= target_temp:
                    if self.heater and (heater_state == True):
                        await self.actor_off(self.heater)

                if sensor_value >= self.cooler_offset_min + target_temp:
                    if self.cooler and (cooler_state == False):
                        await self.actor_on(
                            self.cooler, self.cooler_max_power, self.cooler_max_output
                        )

                if sensor_value <= self.cooler_offset_max + target_temp:
                    if self.cooler and (cooler_state == True):
                        await self.actor_off(self.cooler)

                await asyncio.sleep(1)

        except asyncio.CancelledError as e:
            pass
        except Exception as e:
            logging.error("Fermenter Hysteresis Error {}".format(e))
        finally:
            self.running = False
            if self.heater:
                await self.actor_off(self.heater)
            if self.cooler:
                await self.actor_off(self.cooler)

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
            description="Offset as decimal number when the cooler is switched on. Should be greater then 'CoolerOffsetOff'. For example a value of 2 switches on the cooler if the current temperature is 2 degrees below the target temperature",
        ),
        Property.Number(
            label="CoolerOffsetOff",
            configurable=True,
            description="Offset as decimal number when the cooler is switched off. Should be smaller then 'CoolerOffsetOn'. For example a value of 1 switches off the cooler if the current temperature is 1 degree below the target temperature",
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
class FermenterSpundingHysteresis(CBPiFermenterLogic):
    # subroutine that controls pressure
    async def pressure_control(self):
        self.spunding_offset = float(self.props.get("SpundingOffsetOpen", 0))
        self.valverelease = int(self.props.get("ValveRelease", 1))
        self.pause = int(self.props.get("Pause", 2))
        if self.valve and self.fermenter.pressure_sensor:
            # valve = self.cbpi.actor.find_by_id(self.valve)

            await self.actor_off(self.valve)
            # logging.info("Closing Spunding Valve")

            while self.running:
                target_pressure = float(self.fermenter.target_pressure)
                current_pressure = float(
                    self.get_sensor_value(self.fermenter.pressure_sensor).get("value")
                )
                # logging.info(f'Target: {target_pressure} | Current: {current_pressure}')
                if (
                    current_pressure >= (target_pressure + self.spunding_offset)
                    and target_pressure != 0
                ):
                    while current_pressure >= target_pressure:
                        await self.actor_on(self.valve)
                        await asyncio.sleep(self.valverelease)
                        await self.actor_off(self.valve)
                        await asyncio.sleep(self.pause)
                        current_pressure = float(
                            self.get_sensor_value(self.fermenter.pressure_sensor).get(
                                "value"
                            )
                        )
                        # logging.info("Value higher than target: Spunding loop is running")

                await asyncio.sleep(1)
        else:
            logging.info("No valve or pressure sensor defined")

    async def temperature_control(self):
        self.heater_offset_min = float(self.props.get("HeaterOffsetOn", 0.2))
        self.heater_offset_max = float(self.props.get("HeaterOffsetOff", 0))
        self.cooler_offset_min = float(self.props.get("CoolerOffsetOn", 0.2))
        self.cooler_offset_max = float(self.props.get("CoolerOffsetOff", 0))

        heater = self.cbpi.actor.find_by_id(self.heater)
        cooler = self.cbpi.actor.find_by_id(self.cooler)

        while self.running == True:

            sensor_value = float(
                self.get_sensor_value(self.fermenter.sensor).get("value")
            )
            target_temp = float(self.get_fermenter_target_temp(self.id))

            try:
                heater_state = heater.instance.state
            except:
                heater_state = False
            try:
                cooler_state = cooler.instance.state
            except:
                cooler_state = False

            if sensor_value + self.heater_offset_min <= target_temp:
                if self.heater and (heater_state == False):
                    await self.actor_on(self.heater)

            if sensor_value + self.heater_offset_max >= target_temp:
                if self.heater and (heater_state == True):
                    await self.actor_off(self.heater)

            if sensor_value >= self.cooler_offset_min + target_temp:
                if self.cooler and (cooler_state == False):
                    await self.actor_on(self.cooler)

            if sensor_value <= self.cooler_offset_max + target_temp:
                if self.cooler and (cooler_state == True):
                    await self.actor_off(self.cooler)

            await asyncio.sleep(1)

    async def run(self):
        try:
            self.fermenter = self.get_fermenter(self.id)
            self.heater = self.fermenter.heater
            self.cooler = self.fermenter.cooler
            self.valve = self.fermenter.valve

            pressure_controller = asyncio.create_task(self.pressure_control())
            temperature_controller = asyncio.create_task(self.temperature_control())

            await pressure_controller
            await temperature_controller

        except asyncio.CancelledError as e:
            pass
        except Exception as e:
            logging.error("Fermenter Spunding Hysteresis Error {}".format(e))
        finally:
            self.running = False
            if self.heater:
                await self.actor_off(self.heater)
            if self.cooler:
                await self.actor_off(self.cooler)
            if self.valve:
                await self.actor_off(self.valve)


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
