"""The recipe's figures and the room's temperature given on Brew Day, where neither BeerSmith nor the rig gives them.

A recipe imported before CraftBeerPi kept BeerSmith's mash figures has none: the grain weight and the strike water
it is worked out from are missing, so the strike water box has no USE. And the room's temperature comes from the boil
kettle's probe, so a probe giving no reading, or one no room has, leaves it unknown. The brewer can give each on Brew
Day instead:

- POST /step2/strike/recipe (set_recipe) keeps the grain weight (in lb or kg, as the state's mass_unit) and the
  strike water to the mash tun (in its vol_unit) with the loaded recipe, in its mash_numbers, named in typed and taken
  out of lacks; the forecast is set going on them at once. A figure BeerSmith gave is refused; one given here can be
  given again. Figures no brew comes near, and a body without either, are refused. BeerSmith's figures brought later
  by UPDATE from the Recipe Book's import take the place of those given here.
- Where neither BeerSmith nor Brew Day gives the grain weight, a mash step's own Grain_Kg on Brew Steps stands for it.
- POST /step2/strike/room (set_room) keeps the room's temperature for the brew day: the room while the probe gives no
  reading a room can have, so neither "probe" nor "room" is missing then; once the probe reads within that range, its
  reading is the room and the one given waits aside; null takes it back. A new brew day starts without it.
- The HLT keeps the least water that covers its element and coil (cbpi.strike.vessels.least_water: its own min_water
  on Hardware, else 15 % of its capacity, else 3 gal): mash water given here, or HLT water, that would leave it less is
  refused, saying what would be left and the least; BrewStart's Volume or BeerSmith's figures that do are a warning
  in the state (hlt_low) that blocks nothing.
"""

import asyncio
import sys
import types
from unittest import mock

import pytest

from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller.strike_controller import StrikeController, StrikeRefused
from cbpi.strike import model, vessels


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


HERMS_MODULE = types.ModuleType("strike_typed_pid_herms")
HERMS_MODULE.PIDArduino = object
Herms.__module__ = HERMS_MODULE.__name__

#: Test Recipe v5's mash figures, as a BeerXML import keeps them
BEERSMITH = dict(grain_kg=9.98, infuse_l=34.1, for_grain_l=30.0, tun_kg=5.0, tun_cal_g_c=0.12,
                 infuse_temp_c=57.0, grain_temp_c=18.3, tun_temp_c=22.2, lacks=[])


def kettle(kettle_id, name, logic, sensor, heater, props=None, **measures):
    return types.SimpleNamespace(id=kettle_id, name=name, type=logic, sensor=sensor, heater=heater, agitator=None,
                                 props=Props(props or {}), target_temp=0, instance=None, **measures)


class Brewery:
    """Test Recipe v5 loaded, not begun: BrewStart heating `volume` gal in the HLT (None: no BrewStart), MashIn on the
    Mash Tun at 152 F (with Grain_Kg when step_kg is given), and the boil kettle's probe reading `room` (None: no
    reading). numbers: the recipe's mash_numbers, or none at all. hlt: the HLT's own figures from Hardware (capacity,
    min_water). The strike water helper on it, its worker never started: each job it would set going is kept in
    jobs."""

    def __init__(self, tmp_path, monkeypatch, numbers=None, step_kg=None, room=66.0, volume=15, hlt=None):
        monkeypatch.setitem(sys.modules, HERMS_MODULE.__name__, HERMS_MODULE)
        self.room = room
        self.jobs = []
        monkeypatch.setattr(StrikeController, "_start_job",
                            lambda strike, look, why, quiet=False: self.jobs.append((dict(look.model["brew"]), why)))
        kettles = [kettle("hlt", "HLT", "Hysteresis", "hlt-probe", "hlt-heater", **(hlt or {})),
                   kettle("mt", "Mash Tun", "PID_HERMS", "mt-probe", "hlt-heater", {"HLT_Kettle": "hlt"}),
                   kettle("bk", "Boil Kettle", "BoilPower", "bk-probe", "bk-heater")]
        probe = types.SimpleNamespace(instance=object(), props=Props({}))
        mash_in = {"Kettle": "mt", "Temp": 152}
        if step_kg is not None:
            mash_in["Grain_Kg"] = step_kg
        self.profile = [
            Step("bs", "BrewStart", type="BrewStart", status=StepState.INITIAL,
                 props=Props({"Timing": "Ready by", "Heat_Kettle": "hlt", "Ready_Temp": 160.0, "Volume": volume})),
            Step("mi", "MashIn", type="MashInStep", status=StepState.INITIAL, props=Props(mash_in)),
            Step("rest", "Saccharification", type="MashStep", status=StepState.INITIAL,
                 props=Props({"Kettle": "mt", "Temp": 152}))]
        if volume is None:
            self.profile.pop(0)
        self.basic = {"name": "Test Recipe v5"}
        if numbers is not None:
            self.basic["mash_numbers"] = dict(numbers)
        self.step = types.SimpleNamespace(profile=self.profile, save=mock.AsyncMock(), basic_data=self.basic)
        cbpi = types.SimpleNamespace(
            config=types.SimpleNamespace(get=lambda key, default=None: {"TEMP_UNIT": "F"}.get(key, default)),
            config_folder=Folder(tmp_path), step=self.step,
            kettle=types.SimpleNamespace(data=kettles, set_target_temp=mock.AsyncMock(),
                                         types={"PID_HERMS": {"class": Herms, "properties": [{"label": "HLT_Sensor"}]}}),
            sensor=types.SimpleNamespace(find_by_id=lambda sensor_id: probe, get_sensor_value=self.reading))
        self.strike = StrikeController(cbpi, watch=False)
        self.ids = [s.id for s in self.profile]

    def reading(self, sensor_id):
        return None if self.room is None else {"value": self.room, "age": 1, "max_age": 30}

    def state(self):
        self.strike.update()
        return self.strike.get_state()

    def send(self, command, **body):
        return asyncio.run(getattr(self.strike, command)(dict(body, steps=self.ids)))

    def refused(self, command, **body):
        with pytest.raises(StrikeRefused) as caught:
            self.send(command, **body)
        return caught.value.status, caught.value.body()


# ------------------------------------------------------------------ the recipe's figures

def test_a_recipe_without_beersmiths_figures_takes_the_mash_steps_grain_weight(tmp_path, monkeypatch):
    st = Brewery(tmp_path, monkeypatch, step_kg=9.072).state()
    assert (st["recipe"]["grain"], st["recipe"]["grain_from"], st["recipe"]["strike_vol"]) == (20.0, "step", None)
    assert st["miss"] == "recipe" and st["recipe"]["lacks"] == list(StrikeController.ALL_LACKS)


def test_without_the_mash_steps_either_the_grain_weight_is_missing(tmp_path, monkeypatch):
    st = Brewery(tmp_path, monkeypatch).state()
    assert (st["recipe"]["grain"], st["recipe"]["grain_from"], st["miss"]) == (None, None, "recipe")


def test_the_strike_water_given_is_kept_with_the_recipe_and_the_forecast_set_going(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072)
    b.state()
    out = b.send("set_recipe", strike_vol=9.0)
    numbers = b.basic["mash_numbers"]
    assert numbers["infuse_l"] == round(9.0 * model.L_PER_GAL, 3) and numbers["typed"] == ["water"]
    assert "water" not in numbers["lacks"] and "grain" in numbers["lacks"]
    assert "grain_kg" not in numbers, "the mash step's grain weight isn't written as given"
    b.step.save.assert_awaited()
    st = out["strike"]
    assert (st["miss"], st["misses"], st["recipe"]["strike_vol"], st["recipe"]["typed"]) == (None, [], 9.0, ["water"])
    assert out["applied"] == {"what": "recipe", "strike_vol": 9.0, "from": {"strike_vol": None}}
    brew, why = b.jobs[-1]
    assert brew["strike_gal"] == pytest.approx(9.0, abs=1e-3) and brew["grain_kg"] == pytest.approx(9.072)


def test_both_figures_given_where_the_recipe_has_neither(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch)
    b.state()
    st = b.send("set_recipe", grain=20.0, strike_vol=8.26)["strike"]
    numbers = b.basic["mash_numbers"]
    assert numbers["grain_kg"] == round(20.0 * model.LB, 3) and numbers["typed"] == ["grain", "water"]
    assert numbers["lacks"] == ["tun", "infuse_temp", "grain_temp", "tun_temp"]
    assert (st["miss"], st["recipe"]["grain"], st["recipe"]["grain_from"], st["recipe"]["strike_vol"]) == \
        (None, 20.0, "typed", 8.26)


def test_a_figure_given_here_can_be_given_again(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072)
    b.state()
    b.send("set_recipe", strike_vol=9.0)
    out = b.send("set_recipe", strike_vol=8.5)
    assert out["strike"]["recipe"]["strike_vol"] == 8.5 and out["applied"]["from"] == {"strike_vol": 9.0}
    assert b.basic["mash_numbers"]["typed"] == ["water"]


def test_beersmiths_own_figure_is_refused_and_kept(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    b.state()
    status, body = b.refused("set_recipe", strike_vol=8.0)
    assert (status, body["error"], body["what"]) == (409, "beersmith", "strike_vol")
    assert b.basic["mash_numbers"]["infuse_l"] == 34.1 and "typed" not in b.basic["mash_numbers"]


def test_beersmiths_figures_brought_by_update_replace_those_given_here(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072)
    b.state()
    b.send("set_recipe", strike_vol=9.0)
    # UPDATE from the Recipe Book's import writes the file's mash figures in place of the loaded recipe's
    b.basic["mash_numbers"] = dict(BEERSMITH)
    st = b.state()
    assert (st["miss"], st["recipe"]["typed"], st["recipe"]["grain_from"]) == (None, [], "beersmith")
    assert st["recipe"]["strike_vol"] == round(34.1 / model.L_PER_GAL, 2)
    assert b.refused("set_recipe", strike_vol=9.0)[1]["error"] == "beersmith"


@pytest.mark.parametrize("body, error", [
    (dict(grain=250.0), "out_of_range"),
    (dict(strike_vol=0.2), "out_of_range"),
    (dict(strike_vol=0), "bad_body"),
    (dict(grain="twenty"), "bad_body"),
    (dict(), "bad_body"),
], ids=["grain-too-much", "water-too-little", "water-zero", "grain-text", "neither"])
def test_figures_no_brew_comes_near_are_refused(tmp_path, monkeypatch, body, error):
    b = Brewery(tmp_path, monkeypatch)
    b.state()
    status, answer = b.refused("set_recipe", **body)
    assert (status, answer["error"]) == (400, error)
    assert "mash_numbers" not in b.basic


def test_another_recipes_steps_are_refused(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch)
    b.state()
    with pytest.raises(StrikeRefused) as caught:
        asyncio.run(b.strike.set_recipe(dict(strike_vol=9.0, steps=list(reversed(b.ids)))))
    assert (caught.value.status, caught.value.body()["error"]) == (409, "recipe_changed")
    assert "mash_numbers" not in b.basic


# ------------------------------------------------------------------ the HLT's least water

def test_the_least_is_the_hlts_own_figure_else_a_share_of_its_capacity_else_3_gal():
    def least(unit, vol_unit, **measures):
        return vessels.least_water(types.SimpleNamespace(**measures), unit, vol_unit)

    assert least("F", "gal", min_water=4.5, capacity=20) == {"value": 4.5, "from": "hardware", "capacity": 20.0}
    assert least("F", "gal", capacity=20) == {"value": 3.0, "from": "capacity", "capacity": 20.0}
    assert least("F", "gal", capacity=20, min_water=0) == {"value": 3.0, "from": "capacity", "capacity": 20.0}
    assert least("F", "gal", capacity="lots", min_water="") == {"value": 3.0, "from": "default", "capacity": None}
    assert vessels.least_water(None, "F", "gal") == {"value": 3.0, "from": "default", "capacity": None}
    assert least("C", "L") == {"value": 11.4, "from": "default", "capacity": None}
    assert least("C", "L", capacity=75.0) == {"value": 11.25, "from": "capacity", "capacity": 75.0}
    # BrewStart's Volume_Unit in litres on a F brewery: the HLT's figures, kept in gal, in litres
    assert least("F", "L", min_water=4.0) == {"value": round(4.0 * model.L_PER_GAL, 2), "from": "hardware",
                                              "capacity": None}


def test_the_least_and_the_sparge_left_are_in_the_state(tmp_path, monkeypatch):
    st = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, volume=20, hlt=dict(capacity=20.0)).state()
    assert {k: st["inputs"][k] for k in ("vol", "left", "least", "least_from")} == \
        {"vol": 20.0, "left": round(20.0 - 34.1 / model.L_PER_GAL, 2), "least": 3.0, "least_from": "capacity"}
    assert (st["warnings"], st["misses"]) == ([], [])


def test_mash_water_leaving_the_hlt_less_than_the_least_is_refused_with_what_is_left(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072, volume=20, hlt=dict(capacity=20.0))
    b.state()
    status, body = b.refused("set_recipe", strike_vol=20.0)
    assert (status, body) == (400, {"error": "too_much", "what": "strike_vol", "hlt": 20.0, "left": 0.0,
                                    "least": 3.0, "least_from": "capacity", "most": 17.0})
    assert b.refused("set_recipe", strike_vol=17.01)[1]["left"] == 2.99
    assert "mash_numbers" not in b.basic, "a refused figure isn't kept"
    st = b.send("set_recipe", strike_vol=8.26)["strike"]
    assert (st["recipe"]["strike_vol"], st["inputs"]["left"], st["warnings"]) == (8.26, 11.74, [])
    st = b.send("set_recipe", strike_vol=17.0)["strike"]
    assert (st["inputs"]["left"], st["warnings"]) == (3.0, []), "exactly the least is enough"


def test_the_hlts_own_least_water_is_the_one_mash_water_is_held_to(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072, volume=20, hlt=dict(capacity=20.0, min_water=6.0))
    b.state()
    body = b.refused("set_recipe", strike_vol=14.5)[1]
    assert (body["least"], body["least_from"], body["most"], body["left"]) == (6.0, "hardware", 14.0, 5.5)


def test_mash_water_given_before_the_hlts_water_is_known_is_kept(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072, volume=None)
    assert b.state()["inputs"]["vol"] is None
    st = b.send("set_recipe", strike_vol=20.0)["strike"]
    assert (st["recipe"]["strike_vol"], st["inputs"]["left"], st["warnings"], st["miss"]) == (20.0, None, [], "vol")


def test_hlt_water_leaving_less_than_the_least_after_the_mash_water_is_refused(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, hlt=dict(capacity=20.0))
    b.state()
    mash = round(34.1 / model.L_PER_GAL, 2)
    status, body = b.refused("set_water", vol=11.0)
    assert (status, body) == (400, {"error": "too_little", "min": round(mash + 3.0, 2), "mash": mash,
                                    "left": round(11.0 - mash, 2), "least": 3.0, "least_from": "capacity"})
    assert b.profile[0].props["Volume"] == 15, "a refused figure isn't kept"
    out = b.send("set_water", vol=round(mash + 3.0, 2))
    assert (out["applied"]["to"], out["strike"]["warnings"]) == (round(mash + 3.0, 2), [])


def test_brewstarts_volume_set_elsewhere_too_low_is_a_warning_that_blocks_nothing(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, volume=10, hlt=dict(capacity=20.0))
    st = b.state()
    assert (st["warnings"], st["miss"], st["misses"]) == (["hlt_low"], None, [])
    assert st["inputs"]["left"] == round(10 - 34.1 / model.L_PER_GAL, 2)
    assert b.jobs, "USE is still worked out"
    b.profile[0].props["Volume"] = 12.5
    assert b.state()["warnings"] == []


# ------------------------------------------------------------------ the room's temperature

def test_the_room_given_stands_for_the_room_while_the_probe_gives_no_reading(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, room=None)
    st = b.state()
    assert (st["miss"], st["misses"]) == ("probe", ["probe"])
    out = b.send("set_room", room=66.0)
    st = out["strike"]
    assert out["applied"] == {"what": "room", "to": 66.0, "from": None, "used": True}
    assert (st["miss"], st["misses"]) == (None, [])
    assert {k: st["inputs"][k] for k in ("room", "room_from", "room_given", "room_probe", "grain")} == \
        {"room": 66.0, "room_from": "you", "room_given": 66.0, "room_probe": None, "grain": 66.0}
    assert b.jobs[-1][0]["garage_f"] == 66.0


def test_once_the_probe_reads_a_room_again_it_wins_and_the_room_given_waits(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, room=None)
    b.state()
    b.send("set_room", room=66.0)
    b.room = 64.2
    st = b.state()
    assert {k: st["inputs"][k] for k in ("room", "room_from", "room_given", "room_probe")} == \
        {"room": 64.2, "room_from": "probe", "room_given": 66.0, "room_probe": 64.2}
    b.room = 185.0
    st = b.state()
    assert (st["miss"], st["inputs"]["room"], st["inputs"]["room_from"], st["inputs"]["room_probe"]) == \
        (None, 66.0, "you", 185.0)


def test_the_room_given_taken_back(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, room=185.0)
    b.state()
    b.send("set_room", room=66.0)
    out = b.send("set_room", room=None)
    assert out["applied"] == {"what": "room", "to": None, "from": 66.0, "used": False}
    assert (out["strike"]["miss"], out["strike"]["inputs"]["room_given"]) == ("room", None)


def test_a_room_no_room_has_is_refused(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, room=None)
    b.state()
    status, body = b.refused("set_room", room=150.0)
    assert (status, body["error"], body["low"], body["high"]) == (400, "out_of_range", 35.0, 120.0)


def test_the_room_given_is_for_this_brew_day(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, room=None)
    b.state()
    b.send("set_room", room=66.0)
    b.profile[2] = Step("rest-2", "Saccharification", type="MashStep", status=StepState.INITIAL,
                        props=Props({"Kettle": "mt", "Temp": 152}))
    st = b.state()
    assert (st["inputs"]["room_given"], st["miss"]) == (None, "probe")
