"""The vessels' own figures from the Hardware page in the strike water model (cbpi.strike.vessels).

Each kettle may keep what it weighs empty and what it's made of (empty_weight, material, specific_heat;
cbpi.api.kettle_measures). The rule is the brewer's: a vessel's figure only narrows a guess or replaces BeerSmith's.

- The Mash Tun's figures take the place of the recipe's tun (BeerSmith's TUN_WEIGHT and TUN_SPECIFIC_HEAT) in the
  model: stainless steel 0.12 cal/g C, aluminium 0.22, other at its own specific heat, a weight with no material taken
  as stainless steel. The recipe's own tun stays as the recipe has it.
- The HLT's fix the HLT STEEL guess at their worth in the model's steel, so it leaves the guesses; a number the plant
  file has measured still wins.
- A material alone, a weight that is blank, 0 or less or can't be read, other without a usable specific heat, or a
  material the core doesn't know: nothing, and the model is exactly as without the fields.
- The state's `vessels` says where each came from: "hardware", "beersmith" or none for the tun; "hardware", "guess"
  or "measured" for the HLT.
- A landing keeps it, and the fit of the tun's steel counts the Mash Tun's figures for every landing when it has them.

They fail on 4.7.7.post65, which has no cbpi.strike.vessels.
"""

import asyncio
import json
import sys
import types
from unittest import mock

import pytest

from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller import strike_controller as sc
from cbpi.controller.strike_controller import StrikeController
from cbpi.strike import landings, model, vessels, worker


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


HERMS_MODULE = types.ModuleType("strike_vessels_pid_herms")
HERMS_MODULE.PIDArduino = object
Herms.__module__ = HERMS_MODULE.__name__

#: BeerSmith's tun in Test Recipe v5's equipment profile, as the import keeps it.
TUN_KG, TUN_HEAT = 5.0, 0.12
NUMBERS = dict(grain_kg=9.98, infuse_l=34.1, for_grain_l=30.0, tun_kg=TUN_KG, tun_cal_g_c=TUN_HEAT,
               infuse_temp_c=57.0, grain_temp_c=18.3, tun_temp_c=22.2)


def kettle(kettle_id, name, logic, sensor, heater, props=None, **figures):
    found = types.SimpleNamespace(id=kettle_id, name=name, type=logic, sensor=sensor, heater=heater, agitator=None,
                                  props=Props(props or {}), target_temp=0, instance=None)
    for field, value in figures.items():
        setattr(found, field, value)
    return found


def helper(tmp_path, monkeypatch, mt=None, hlt=None, unit="F", plant=None):
    """The strike water helper on a brew day it can work USE out for: BrewStart heating all 15 gal in the HLT, MashIn
    on the Mash Tun at 152 F, the room at 66 F. `mt` and `hlt`: each kettle's figures, as attributes; None leaves
    them off the kettle altogether, as on a core before them."""
    monkeypatch.setitem(sys.modules, HERMS_MODULE.__name__, HERMS_MODULE)
    kettles = [kettle("hlt", "HLT", "Hysteresis", "hlt-probe", "hlt-heater", **(hlt or {})),
               kettle("mt", "Mash Tun", "PID_HERMS", "mt-probe", "hlt-heater", {"HLT_Kettle": "hlt"}, **(mt or {})),
               kettle("bk", "Boil Kettle", "BoilPower", "bk-probe", "bk-heater")]
    room = 66.0 if unit == "F" else 18.9
    probe = types.SimpleNamespace(instance=object(), props=Props({}))
    profile = [Step("bs", "BrewStart", type="BrewStart", status=StepState.INITIAL,
                    props=Props({"Timing": "Ready by", "Heat_Kettle": "hlt", "Ready_Temp": 160.0,
                                 "Volume": 15 if unit == "F" else 57})),
               Step("mi", "MashIn", type="MashInStep", status=StepState.INITIAL,
                    props=Props({"Kettle": "mt", "Temp": 152 if unit == "F" else 66.7})),
               Step("rest", "Saccharification", type="MashStep", status=StepState.INITIAL,
                    props=Props({"Kettle": "mt", "Temp": 152 if unit == "F" else 66.7}))]
    settings = {"TEMP_UNIT": unit}
    cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: settings.get(key, default)),
        config_folder=Folder(tmp_path),
        step=types.SimpleNamespace(profile=profile, save=mock.AsyncMock(),
                                   basic_data={"name": "Test Recipe v5", "mash_numbers": dict(NUMBERS)}),
        kettle=types.SimpleNamespace(data=kettles, set_target_temp=mock.AsyncMock(),
                                     types={"PID_HERMS": {"class": Herms, "properties": [{"label": "HLT_Sensor"}]}}),
        sensor=types.SimpleNamespace(find_by_id=lambda sensor_id: probe,
                                     get_sensor_value=lambda sensor_id: {"value": room, "age": 1, "max_age": 30}))
    if plant is not None:
        (tmp_path / StrikeController.PLANT_FILE).write_text(json.dumps(plant), encoding="utf-8")
    return StrikeController(cbpi, watch=False)


def look(tmp_path, monkeypatch, **kwargs):
    tmp_path.mkdir(exist_ok=True)
    found = helper(tmp_path, monkeypatch, **kwargs)._look()
    assert found.miss is None and found.off is None, (found.miss, found.off)
    return found


def tun_in(found):
    brew = found.model["brew"]
    return brew["tun_kg"], brew["tun_cal_g_c"]


def hlt_steel(found):
    return found.model["plant"]["guesses"]["hlt_steel"]


NONE = {"from": None, "empty_weight": None, "material": None, "specific_heat": None}


# ------------------------------------------------------------------ nothing from Hardware: exactly as before

@pytest.mark.parametrize("blank", [
    None,
    dict(empty_weight=None, material=None, specific_heat=None, capacity=None),
    dict(empty_weight=0, material="stainless_steel"),
    dict(empty_weight=-3, material="stainless_steel"),
    dict(empty_weight="twenty", material="stainless_steel"),
    dict(empty_weight=None, material="aluminium"),
    dict(empty_weight=33, material="copper"),
    dict(empty_weight=33, material="other"),
    dict(empty_weight=33, material="other", specific_heat=0),
    dict(empty_weight=33, material="other", specific_heat=2.0),
], ids=["no-fields", "all-blank", "zero", "negative", "text", "material-alone", "unknown-material",
        "other-without-heat", "other-zero-heat", "other-more-than-water"])
def test_a_vessel_with_no_usable_figure_leaves_the_model_as_it_was(tmp_path, monkeypatch, blank):
    before = look(tmp_path / "a", monkeypatch)
    found = look(tmp_path / "b", monkeypatch, mt=blank, hlt=blank)
    assert found.model == before.model and found.key == before.key, "the model's inputs changed"
    assert tun_in(found) == (TUN_KG, TUN_HEAT), "BeerSmith's tun is the model's, as before"
    assert hlt_steel(found)["provenance"] == model.ASSUMED and not hlt_steel(found)["low"] == hlt_steel(found)["high"]
    assert found.vessels == {"mt": dict(NONE, **{"from": "beersmith", "empty_weight": 11.0, "specific_heat": 0.12}),
                             "hlt": dict(NONE, **{"from": "guess"})}
    assert "hlt_steel" in [g["name"] for g in found.guesses]


def test_without_a_tun_in_the_recipe_or_on_the_mash_tun_none_is_counted(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch)
    strike.cbpi.step.basic_data["mash_numbers"].update(tun_kg=0.0, tun_cal_g_c=0.0)
    found = strike._look()
    assert tun_in(found) == (0.0, 0.0) and found.vessels["mt"] == NONE


# ------------------------------------------------------------------ the Mash Tun's figures

@pytest.mark.parametrize("figures, heat, material", [
    (dict(empty_weight=33, material="stainless_steel"), 0.12, "stainless_steel"),
    (dict(empty_weight=33, material="aluminium"), 0.22, "aluminium"),
    (dict(empty_weight=33, material="other", specific_heat=0.09), 0.09, "other"),
    (dict(empty_weight=33), 0.12, None),
    (dict(empty_weight="33", material=""), 0.12, None),
], ids=["stainless", "aluminium", "other", "no-material", "text-weight-blank-material"])
def test_the_mash_tuns_figures_take_the_place_of_beersmiths_tun(tmp_path, monkeypatch, figures, heat, material):
    found = look(tmp_path, monkeypatch, mt=figures)
    kg, cal = tun_in(found)
    assert kg == pytest.approx(33 * model.LB) and cal == heat
    assert found.recipe["tun"] == 11.0 and found.recipe["tun_heat"] == TUN_HEAT, "the recipe's own tun stays"
    assert found.vessels["mt"] == {"from": "hardware", "empty_weight": 33.0, "material": material,
                                   "specific_heat": heat}


def test_a_new_figure_works_use_out_again(tmp_path, monkeypatch):
    before = look(tmp_path / "a", monkeypatch)
    after = look(tmp_path / "b", monkeypatch, mt=dict(empty_weight=33, material="stainless_steel"))
    assert after.key != before.key


def test_on_a_celsius_brewery_the_weight_is_in_kilograms(tmp_path, monkeypatch):
    found = look(tmp_path, monkeypatch, unit="C", mt=dict(empty_weight=15, material="stainless_steel"))
    assert tun_in(found) == (15, 0.12)
    assert found.vessels["mt"]["empty_weight"] == 15.0


# ------------------------------------------------------------------ the HLT's figures

def test_the_hlts_figures_fix_the_hlt_steel_guess(tmp_path, monkeypatch):
    found = look(tmp_path, monkeypatch, hlt=dict(empty_weight=40, material="stainless_steel"))
    steel = round(40 * model.LB * 0.12 * model.CAL_G_C / model.STEEL_C, 3)
    r = hlt_steel(found)
    assert (r["low"], r["central"], r["high"]) == (steel, steel, steel) and r["provenance"] == model.DERIVED
    assert "hlt_steel" not in [g["name"] for g in found.guesses], "a fixed number isn't a guess on the screen"
    assert "hlt_steel" not in [m["name"] for m in found.measured], "nor something the rig has measured"
    assert found.vessels["hlt"] == {"from": "hardware", "empty_weight": 40.0, "material": "stainless_steel",
                                    "specific_heat": 0.12}
    assert tun_in(found) == (TUN_KG, TUN_HEAT), "the HLT's figures don't touch the tun"


def test_a_measured_hlt_steel_wins_over_the_hlts_figures(tmp_path, monkeypatch):
    plant = sc.default_plant_file()
    plant["guesses"]["hlt_steel"]["measured"] = {"value": 12.5, "date": "2026-11-20", "method": "weighed"}
    found = look(tmp_path, monkeypatch, plant=plant, hlt=dict(empty_weight=40, material="stainless_steel"))
    r = hlt_steel(found)
    assert (r["low"], r["high"], r["provenance"]) == (12.5, 12.5, model.MEASURED)
    assert found.vessels["hlt"] == dict(NONE, **{"from": "measured"})


def test_the_state_says_where_the_vessels_figures_came_from(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, mt=dict(empty_weight=33), hlt=dict(empty_weight=40))
    state = strike.get_state()
    assert state["vessels"]["mt"]["from"] == "hardware" and state["vessels"]["hlt"]["from"] == "hardware"
    assert state["mass_unit"] == "lb"


# ------------------------------------------------------------------ the landing, and the fit (S2)

def test_a_landing_keeps_where_the_tun_came_from(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, mt=dict(empty_weight=33, material="stainless_steel"))
    found = strike._look()
    lo, hi = [150.0] * 61, [153.0] * 61
    strike.answer = dict(plan="p", unit="F", use=160.0, numbers=dict(rest=152.0, key="k"),
                         forecasts={"160.0": dict(lo=lo, hi=hi, searched=True)}, _job={})
    found.inputs["planned"] = 160.0
    record, why = strike._landing(found)
    assert why is None and record["vessels"] == found.vessels and record["vessels"]["mt"]["from"] == "hardware"


def test_the_fit_counts_the_mash_tuns_figures_for_every_landing(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, mt=dict(empty_weight=33, material="stainless_steel"))
    assert strike._fit_tun() == [round(33 * model.LB, 4), 0.12]
    (tmp_path / "none").mkdir()
    assert helper(tmp_path / "none", monkeypatch)._fit_tun() is None


def test_a_landings_curves_are_worked_out_again_with_the_mash_tuns_figures(tmp_path, monkeypatch):
    strike = helper(tmp_path, monkeypatch, mt=dict(empty_weight=33, material="stainless_steel"))
    record = dict(version=landings.VERSION, t=1795327740.0, steps=["bs", "mi", "rest"], name="v5", unit="F",
                  strike=160.0, rest=152.0, numbers=dict(probe="tun"), sensor="mt-probe",
                  job=dict(brew=dict(probe="tun")))
    (tmp_path / landings.LANDINGS_FILE).write_text(json.dumps(record) + "\n", encoding="utf-8")
    key = landings.file_key(str(tmp_path / landings.LANDINGS_FILE))
    curves = dict(mid=[124.0] * len(landings.GRID), lo0=[122.0] * len(landings.GRID), hi0=[126.0] * len(landings.GRID))
    lid = landings.landing_id(record)
    # Fitted before the Mash Tun had its figures: the recipe's tun, its landing found.
    strike.fit = dict(key=key, tun=None, landings=[dict(id=lid, landed=124.1, tun=None, **curves)])
    sent = []

    async def send(sensors, message):
        sent.append(message)
    monkeypatch.setattr(strike, "_send_fit", send)

    async def go():
        strike._fit_check()
        await asyncio.sleep(0)
    asyncio.run(go())
    assert sent, "the fit wasn't worked out again for the Mash Tun's figures"
    item = sent[0]["landings"][0]
    assert item["curves"] is True and item["tun"] == [round(33 * model.LB, 4), 0.12]


def test_the_workers_curves_count_the_tun_given(monkeypatch):
    seen = []

    def job_from(job):
        seen.append(job["brew"])
        raise RuntimeError("enough")
    monkeypatch.setattr(worker, "job_from", job_from)
    record = dict(job=dict(brew=dict(tun_kg=5.0, tun_cal_g_c=0.12, probe="tun")), unit="F", strike=160.0)
    for tun in (None, [14.97, 0.22]):
        with pytest.raises(RuntimeError):
            worker.landing_curves(record, tun=tun)
    assert seen == [dict(tun_kg=5.0, tun_cal_g_c=0.12, probe="tun"), dict(tun_kg=14.97, tun_cal_g_c=0.22, probe="tun")]


def test_figure_never_raises_on_an_odd_kettle():
    class Odd:
        @property
        def empty_weight(self):
            raise RuntimeError("no")
    assert vessels.figure(Odd(), "F") is None
    assert vessels.figure(None, "F") is None
