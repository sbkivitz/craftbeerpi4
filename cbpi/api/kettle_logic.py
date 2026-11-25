import asyncio
import inspect
import logging
from abc import ABCMeta

from cbpi.api.base import CBPiBase
from cbpi.api.extension import CBPiExtension


class CBPiKettleLogic(CBPiBase, metaclass=ABCMeta):
    """A kettle's AUTO: what drives its element, and its agitator, toward its target.

    A logic may define clock_moved(self, seconds): told, while it runs, that the
    brewing clock's now() has just jumped by `seconds` - on a simulator whose
    speed changed, which takes the clock back to the computer's time
    (CraftBeerPi.brewing_clock; never on a rig). It moves whatever it keeps on
    now() by as much, and runs on. A running logic without it is stopped and
    started again instead, as AUTO off and on would (KettleController.
    restart_after_clock_reset): kept times on a clock that went back would hold
    a PID's output until the clock caught up.
    """

    def __init__(self, cbpi, id, props):
        self.cbpi = cbpi
        self.id = id
        self.props = props
        self.state = False
        self.running = False

    def init(self):
        pass

    @classmethod
    def blank_defaults(cls, unit):
        """What this logic uses for each of its settings left empty, in `unit`:
        {setting label: value}.

        A setting can be left empty, and the logic then uses a value of its
        own. A setting's declared default_value is only what a new kettle form
        starts with: it can be missing, or wrong, and it can't follow the
        temperature unit. This says what an empty setting really becomes, so
        the kettle form and a settings file can show it, and fill it in, before
        the kettle is saved.

        - `unit` is the configured temperature unit, "C" or "F". A value that
          depends on it is given in it.
        - One entry for each declared setting whose empty value the logic
          replaces with a value of its own: that value, exactly as the logic
          uses it - a number, or one of a choice's options. Read it from the
          same table or function the logic itself reads when it runs, so the
          two can't drift apart.
        - Left out: a setting whose empty value means something other than a
          value - "use another setting", "find it another way", "leave
          something as it is" - because filling it in would change what the
          logic does; a setting whose value depends on another setting, not
          only on the unit; and links to a sensor, actor, kettle or fermenter.
        - Values are finite numbers, text or true/false, so they can be sent
          as JSON.
        - It is asked on the class, with no kettle and no running logic, so
          it must not touch CraftBeerPi. It shouldn't raise.

        KettleController.get_types() sends it with each logic, in the
        configured unit. An entry it can't use is left out, and a hook that
        raises counts as {}.

        This one knows nothing about the settings: {}.
        """
        return {}

    async def on_start(self):
        pass

    async def actor_on(self, id, power=100, output=None):
        """CBPiBase.actor_on, unless this is the mash tun's AUTO and the brewer hasn't said the strike water is in it:
        then its wort pump and the heat it sends through the coil stay off, and he is told
        (StepController.strike_move_refuses)."""
        refuses = getattr(getattr(self.cbpi, "step", None), "strike_move_refuses", None)
        if inspect.iscoroutinefunction(refuses):
            try:
                if await refuses(id, logic_kettle=self.id) is True:
                    return
            except Exception as e:  # noqa: BLE001 - not knowing is no permission
                logging.getLogger(type(self).__name__).warning(
                    "Could not tell whether %s may switch %s on, so it stays off: %s", self.id, id, e)
                return
        await super().actor_on(id, power, output)

    async def on_stop(self):
        pass

    async def run(self):
        pass

    async def _run(self):

        try:
            await self.on_start()
            self.cancel_reason = await self.run()
        except asyncio.CancelledError as e:
            pass
        finally:
            await self.on_stop()

    def get_state(self):
        return dict(running=self.state)

    def log_data(self, series, value):
        """Write a control signal to the log store, under its own series name.

        CBpiActor and CBpiSensor both have log_data(); this is the kettle
        logic's, so a control signal can be recorded and a PID evaluated after
        the fact. The temperature log shows what a logic produced, not the
        duty it asked for - and those two curves are what separate "badly
        tuned" from "undersized element" from "laggy probe", which look alike
        from the temperature alone.

        `series` is appended to this kettle's id, so one logic can record
        several signals - duty, setpoint, and for a cascade an inner pair -
        without colliding with the kettle's own sensor. The resulting name is
        just an id as far as the log store is concerned, so the existing
        rotation, batching, retention and charting all apply with no new
        plumbing: /log/<kettleid>.<series> returns it like any sensor.

        Deliberately not gated here. Whether to log at all is a policy question
        that belongs to the caller, which knows whether the brewer asked for
        it; a base class that silently dropped writes would be worse than one
        that just writes.
        """
        try:
            series_id = "{}.{}".format(self.id, series)
            # Not log_data(): that resolves the id to a registered sensor to
            # get a display name and silently returns when it cannot, so every
            # control write would be dropped without a word.
            self.cbpi.log.log_control_data(series_id, value, series_id)
        except Exception as e:  # noqa: BLE001 - telemetry never breaks control
            logging.getLogger(type(self).__name__).warning(
                "Could not log %s for %s: %s", series, self.id, e
            )

    def pid_logging_enabled(self):
        """Has the brewer asked for control signals to be recorded?

        Read every call rather than cached, so switching it on mid-brew starts
        recording without a restart - which is the point of a knob. It is a
        dictionary lookup against the config cache, not a file read.
        """
        try:
            return str(self.cbpi.config.get("PID_LOGGING", "No")) == "Yes"
        except Exception:  # noqa: BLE001
            return False

    async def start(self):

        self.state = True

    async def stop(self):

        self.task.cancel()
        await self.task
        self.state = False
