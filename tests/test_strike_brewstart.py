"""BrewStart 1.3.0 as the strike water step.

From BrewStart 1.3.0 a Ready by BrewStart heats the HLT itself, holds the water at its Ready_Temp and asks for it to
be moved, and only the brewer's Next moves on. With no strike water step on the HLT before the Mash Tun's MashIn -
Test Recipe v5 - CraftBeerPi's strike water helper takes that BrewStart as the strike water step:

- the state says by "step", step_ids.strike is BrewStart's, and inputs.planned is its Ready_Temp; the phase is "plan"
  while it waits for its time, "heat" once it has begun heating (its mark, _brewstart_heating) and isn't done, and
  "moved" once it is done or the Mash Tun's MashIn has started;
- USE and BACK TO write its Ready_Temp: through change_target while it runs, which moves the HLT's target once it
  heats and takes down a question already asked; through forget_wait when it was stopped after it asked;
- an older BrewStart (no change_target), a Start at, or a Ready by on another kettle isn't: the helper works by hand,
  as before;
- a recipe with a strike water step on the HLT keeps that step as the strike water step, as before.

Those taking BrewStart as the strike water step fail on 4.7.7.post60.
"""

import asyncio
import sys
import types
from unittest import mock

import pytest

from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller.strike_controller import StrikeController


class Folder:
    def __init__(self, root):
        self.root = root

    def get_file_path(self, name):
        return str(self.root / name)


class Herms:
    """PID_HERMS as the strike water helper knows it: OUTER_LOOP_RATIO, and PIDArduino in its module."""
    OUTER_LOOP_RATIO = 4

    @staticmethod
    def blank_defaults(unit):
        return {}


HERMS_MODULE = types.ModuleType("strike_test_pid_herms")
HERMS_MODULE.PIDArduino = object
Herms.__module__ = HERMS_MODULE.__name__


class BrewStart13:
    """A BrewStart 1.3.0 step instance, as far as the strike water helper reaches into it."""
    HEATING_PROP = "_brewstart_heating"

    def __init__(self, props, heating=False, asked=False):
        self.props, self.heating, self.asked = props, heating, asked
        self.calls = []

    async def change_target(self, temp):
        self.calls.append(("change_target", temp))
        self.props["Ready_Temp"] = temp
        asked, self.asked = self.asked, False
        return asked and self.heating

    def forget_wait(self):
        self.calls.append(("forget_wait",))
        asked, self.asked = self.asked, False
        return asked


class BrewStartPost10:
    """A BrewStart before 1.3.0: it hands over at the heating start, and has no change_target."""

    def __init__(self, props, **kwargs):
        self.props = props


def kettle(kettle_id, name, logic, sensor, heater, props=None):
    return types.SimpleNamespace(id=kettle_id, name=name, type=logic, sensor=sensor, heater=heater, agitator=None,
                                 props=Props(props or {}), target_temp=0, instance=None)


def helper(tmp_path, monkeypatch, profile):
    monkeypatch.setitem(sys.modules, HERMS_MODULE.__name__, HERMS_MODULE)
    kettles = [kettle("hlt", "HLT", "Hysteresis", "hlt-probe", "hlt-heater"),
               kettle("mt", "Mash Tun", "PID_HERMS", "mt-probe", "hlt-heater", {"HLT_Kettle": "hlt"}),
               kettle("bk", "Boil Kettle", "BoilPower", None, "bk-heater")]
    cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: {"TEMP_UNIT": "F"}.get(key, default)),
        config_folder=Folder(tmp_path),
        step=types.SimpleNamespace(profile=profile, basic_data={"name": "Test Recipe v5"}, save=mock.AsyncMock()),
        kettle=types.SimpleNamespace(data=kettles, set_target_temp=mock.AsyncMock(),
                                     types={"PID_HERMS": {"class": Herms, "properties": [{"label": "HLT_Sensor"}]}}),
        sensor=types.SimpleNamespace(find_by_id=lambda sensor_id: None, get_sensor_value=lambda sensor_id: None))
    return StrikeController(cbpi, watch=False)


def brewstart(status=StepState.ACTIVE, heating=False, asked=False, timing="Ready by", heat_kettle="hlt",
              instance=BrewStart13):
    props = Props({"Timing": timing, "Heat_Kettle": heat_kettle, "Ready_Temp": 152.0, "Volume": 15})
    if heating:
        props[BrewStart13.HEATING_PROP] = True
    step = Step("bs", "BrewStart", type="BrewStart", status=status, props=props)
    step.instance = instance(props, heating=heating, asked=asked) if instance is not None else None
    return step


def mash_tun(status=StepState.INITIAL):
    return [Step("mi", "MashIn", type="MashInStep", status=status, props=Props({"Kettle": "mt", "Temp": 126})),
            Step("rest", "Protein Rest", type="MashStep", status=StepState.INITIAL,
                 props=Props({"Kettle": "mt", "Temp": 126}))]


def v5(**kwargs):
    """Test Recipe v5: BrewStart, then MashIn on the Mash Tun."""
    return [brewstart(**kwargs)] + mash_tun()


def state(strike):
    return strike.get_state(strike._look())


def apply(strike, do, to):
    look = strike._look()
    strike.day["steps"] = list(look.ids)
    return asyncio.run(strike._apply_strike(look, do, to))


# ------------------------------------------------------------------ the state

def test_a_ready_by_brewstart_that_heats_the_hlt_is_the_strike_water_step(tmp_path, monkeypatch):
    found = state(helper(tmp_path, monkeypatch, v5()))
    assert (found["applies"], found["by"], found["phase"]) == (True, "step", "plan")
    assert found["step_ids"] == {"brewstart": "bs", "strike": "bs", "mash_in": "mi"}
    assert found["inputs"]["planned"] == 152.0


@pytest.mark.parametrize("status, heating, mash_in, phase", [
    (StepState.ACTIVE, False, StepState.INITIAL, "plan"),     # waiting for its time
    (StepState.ACTIVE, True, StepState.INITIAL, "heat"),
    (StepState.STOP, True, StepState.INITIAL, "heat"),        # stopped while it heated
    (StepState.STOP, False, StepState.INITIAL, "plan"),
    (StepState.DONE, True, StepState.INITIAL, "moved"),       # Next: the water is in the tun
    (StepState.DONE, True, StepState.ACTIVE, "moved"),
])
def test_its_phase(tmp_path, monkeypatch, status, heating, mash_in, phase):
    profile = [brewstart(status=status, heating=heating)] + mash_tun(mash_in)
    assert state(helper(tmp_path, monkeypatch, profile))["phase"] == phase


@pytest.mark.parametrize("bs", [
    dict(instance=BrewStartPost10),                           # hands over at the heating start
    dict(timing="Start at"),
    dict(heat_kettle="bk"),
    dict(instance=None),
], ids=["before-1.3.0", "start-at", "another-kettle", "no-instance"])
def test_a_brewstart_that_doesnt_heat_the_hlt_itself_isnt(tmp_path, monkeypatch, bs):
    found = state(helper(tmp_path, monkeypatch, v5(**bs)))
    assert (found["by"], found["step_ids"]["strike"], found["step_ids"]["brewstart"]) == ("hand", None, "bs")
    assert found["inputs"]["planned"] is None


def test_a_strike_water_step_on_the_hlt_stays_the_strike_water_step(tmp_path, monkeypatch):
    strike_water = Step("sw", "Strike water", type="MashInStep", status=StepState.INITIAL,
                        props=Props({"Kettle": "hlt", "Temp": 163.0}))
    profile = [brewstart(), strike_water] + mash_tun()
    strike = helper(tmp_path, monkeypatch, profile)
    found = state(strike)
    assert (found["by"], found["step_ids"]["strike"], found["inputs"]["planned"]) == ("step", "sw", 163.0)
    applied = apply(strike, "use", 160.0)
    assert applied["changed"] == ["step", "brewstart"]
    assert (strike_water.props["Temp"], profile[0].props["Ready_Temp"]) == (160.0, 160.0)
    assert profile[0].instance.calls == []


# ------------------------------------------------------------------ USE and BACK TO

def test_use_while_it_heats_goes_through_change_target(tmp_path, monkeypatch):
    profile = v5(heating=True, asked=True)
    strike = helper(tmp_path, monkeypatch, profile)
    applied = apply(strike, "use", 155.0)
    assert (applied["by"], applied["from"], applied["to"]) == ("step", 152.0, 155.0)
    assert applied["changed"] == ["step", "question", "hlt", "brewstart"]
    assert profile[0].instance.calls == [("change_target", 155.0)]
    assert profile[0].props["Ready_Temp"] == 155.0
    strike.cbpi.kettle.set_target_temp.assert_not_awaited()  # BrewStart moves the HLT's target itself
    assert strike.day["used"] == [dict(step="bs", prop="Ready_Temp", value=155.0, unit="F")]
    assert strike.recipe_temps(profile) == {"bs": {"Ready_Temp": 152.0}}
    strike.cbpi.step.save.assert_awaited()


def test_use_while_it_waits_for_its_time_changes_only_its_ready_temp(tmp_path, monkeypatch):
    profile = v5()
    strike = helper(tmp_path, monkeypatch, profile)
    assert apply(strike, "use", 150.4)["changed"] == ["step", "brewstart"]
    assert profile[0].instance.calls == [("change_target", 150.4)] and profile[0].props["Ready_Temp"] == 150.4


def test_use_on_one_stopped_after_it_asked_forgets_the_question(tmp_path, monkeypatch):
    profile = v5(status=StepState.STOP, heating=True, asked=True)
    strike = helper(tmp_path, monkeypatch, profile)
    assert apply(strike, "use", 156.0)["changed"] == ["step", "question", "brewstart"]
    assert profile[0].instance.calls == [("forget_wait",)] and profile[0].props["Ready_Temp"] == 156.0


def test_back_to_puts_its_ready_temp_back(tmp_path, monkeypatch):
    profile = v5(heating=True)
    strike = helper(tmp_path, monkeypatch, profile)
    apply(strike, "use", 155.0)
    applied = apply(strike, "back", 152.0)
    assert applied["changed"] == ["step", "hlt", "brewstart"] and profile[0].props["Ready_Temp"] == 152.0
    assert strike.day["used"] is None and strike.day["was"] is None
