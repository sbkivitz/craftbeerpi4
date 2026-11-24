"""Every number missing in the strike water state (`misses`), beside the first (`miss`).

The strike water box can't work USE out while a number is missing, and says which. `miss` names the first, in the
design's order (recipe, sample_time, probe, room, grain, vol); a screen that reads only it can't learn of one further
down, such as a missing water figure behind any other. `misses` lists every one in that order, `miss` first, so the
box can ask for the water whenever it is missing:

- nothing missing: none, and `miss` null;
- no water figure behind a recipe without BeerSmith's mash figures, a probe giving no reading, and one no room has;
- too little water (less than the recipe's strike water) behind the probe; without the recipe's strike water, too
  little can't be told, so only no water figure at all is listed;
- one missing alone: just it, as `miss` has it.
"""

import sys
import types
from unittest import mock

import pytest

from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller.strike_controller import StrikeController


class Folder:
    def __init__(self, root):
        self.root = root
        self.logsFolderPath = str(root / "logs")

    def get_file_path(self, name):
        return str(self.root / name)


class Herms:
    OUTER_LOOP_RATIO = 4

    @staticmethod
    def blank_defaults(unit):
        return {}


HERMS_MODULE = types.ModuleType("strike_misses_pid_herms")
HERMS_MODULE.PIDArduino = object
Herms.__module__ = HERMS_MODULE.__name__

#: Test Recipe v5's mash figures, as a BeerXML import keeps them: 34.1 L (9.01 gal) of strike water
NUMBERS = dict(grain_kg=9.98, infuse_l=34.1, for_grain_l=30.0, tun_kg=5.0, tun_cal_g_c=0.12,
               infuse_temp_c=57.0, grain_temp_c=18.3, tun_temp_c=22.2)


def kettle(kettle_id, name, logic, sensor, heater, props=None):
    return types.SimpleNamespace(id=kettle_id, name=name, type=logic, sensor=sensor, heater=heater, agitator=None,
                                 props=Props(props or {}), target_temp=0, instance=None)


def state(tmp_path, monkeypatch, volume=15, room=66.0, numbers=True):
    """The strike water state on Test Recipe v5 not begun: BrewStart heating the HLT's water (volume, "" for none),
    MashIn on the Mash Tun at 152 F, the boil kettle's probe reading room (None: no reading), and the recipe's mash
    figures or none."""
    monkeypatch.setitem(sys.modules, HERMS_MODULE.__name__, HERMS_MODULE)
    kettles = [kettle("hlt", "HLT", "Hysteresis", "hlt-probe", "hlt-heater"),
               kettle("mt", "Mash Tun", "PID_HERMS", "mt-probe", "hlt-heater", {"HLT_Kettle": "hlt"}),
               kettle("bk", "Boil Kettle", "BoilPower", "bk-probe", "bk-heater")]
    probe = types.SimpleNamespace(instance=object(), props=Props({}))
    profile = [Step("bs", "BrewStart", type="BrewStart", status=StepState.INITIAL,
                    props=Props({"Timing": "Ready by", "Heat_Kettle": "hlt", "Ready_Temp": 160.0, "Volume": volume})),
               Step("mi", "MashIn", type="MashInStep", status=StepState.INITIAL, props=Props({"Kettle": "mt", "Temp": 152})),
               Step("rest", "Saccharification", type="MashStep", status=StepState.INITIAL,
                    props=Props({"Kettle": "mt", "Temp": 152}))]
    basic = {"name": "Test Recipe v5"}
    if numbers:
        basic["mash_numbers"] = dict(NUMBERS)
    reading = None if room is None else {"value": room, "age": 1, "max_age": 30}
    cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: {"TEMP_UNIT": "F"}.get(key, default)),
        config_folder=Folder(tmp_path),
        step=types.SimpleNamespace(profile=profile, save=mock.AsyncMock(), basic_data=basic),
        kettle=types.SimpleNamespace(data=kettles, set_target_temp=mock.AsyncMock(),
                                     types={"PID_HERMS": {"class": Herms, "properties": [{"label": "HLT_Sensor"}]}}),
        sensor=types.SimpleNamespace(find_by_id=lambda sensor_id: probe, get_sensor_value=lambda sensor_id: reading))
    st = StrikeController(cbpi, watch=False).get_state()
    assert st["applies"], st["off"]
    return st


@pytest.mark.parametrize("given, miss, misses", [
    (dict(), None, []),
    (dict(volume=""), "vol", ["vol"]),
    (dict(numbers=False), "recipe", ["recipe"]),
    (dict(numbers=False, volume=""), "recipe", ["recipe", "vol"]),
    (dict(room=None, volume=""), "probe", ["probe", "vol"]),
    (dict(room=185.0, volume=""), "room", ["room", "vol"]),
    (dict(room=None, volume=5), "probe", ["probe", "vol"]),
    (dict(numbers=False, volume=5), "recipe", ["recipe"]),
    (dict(numbers=False, room=None), "recipe", ["recipe", "probe"]),
], ids=["nothing", "water-alone", "recipe-alone", "water-behind-recipe", "water-behind-probe", "water-behind-room",
        "too-little-behind-probe", "too-little-without-the-recipes-strike", "probe-behind-recipe"])
def test_every_number_missing_is_listed_the_first_as_miss(tmp_path, monkeypatch, given, miss, misses):
    st = state(tmp_path, monkeypatch, **given)
    assert (st["miss"], st["misses"]) == (miss, misses)
