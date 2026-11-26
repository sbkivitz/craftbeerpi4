"""BeerSmith's strike temperature drives BrewStart and MashIn.

BeerSmith's strike already pays for warming a cold tun's steel ("Adjust Temp for Equip"). BrewStart heats the HLT to
it, and the recipe's dough-in MashIn on the mash tun then heats the moved water only to where that strike water settles
once it has warmed the steel BeerSmith counted (cbpi.api.strike.dough_in_temperature), and asks for the malt there, so
the steel isn't paid for twice. The strike water helper says which strike is in use and where it came from (the
state's strike), takes the brewer's own (set) and BeerSmith's again (beersmith) until the malt is asked for, moving only
MashIn's pre-heat once the water has moved, and gives CraftBeerPi's forecast as a check (the state's check) that never
blocks anything. An UPDATE import moves a BrewStart's Ready_Temp that still holds BeerSmith's old strike, and only
that. The LOG says a mash water or grain weight changed in the strike water card's words, wherever it was changed.
Each test fails on 4.7.7.post94.
"""

import asyncio
import math
import sys
import types
import xml.etree.ElementTree as ET
from unittest import mock

import pytest
import yaml

from cbpi.api import strike as bs
from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller.recipe_controller import RecipeController
from cbpi.controller.strike_controller import StrikeController, StrikeRefused
from cbpi.controller.upload_controller import UploadController
from cbpi.extension.mashstep import MashInStep
from cbpi.strike import model

# Test Recipe v6c and v6 as BeerSmith exports them: 20 lb of grain at 72 F, a 33 lb stainless tun at 72 F, the 152 F
# rest; v6c with 78.04 qt of mash water (157.7 F), v6 with 28.04 qt (165.3 F).
V6C = {"grain_kg": 9.07184, "grain_temp_c": 22.2222, "infuse_l": 73.853384, "for_grain_l": 70.97688,
       "infuse_temp_c": 69.8333, "tun_cal_g_c": 0.12, "tun_kg": 14.968536, "tun_temp_c": 22.2222, "rest_c": 66.6667,
       "equip_adjust": True, "lacks": []}
V6 = dict(V6C, infuse_l=26.535737, for_grain_l=23.65896, infuse_temp_c=74.0556)


def beerxml(infuse_temp="157.7 F", step_temp="66.6666667", shown="78.04 qt", amount="70.9768800", adjust="TRUE"):
    return ET.fromstring("""<RECIPES><RECIPE><NAME>Craftbeerpi Test Recipe v6c</NAME>
<FERMENTABLES><FERMENTABLE><NAME>Pale</NAME><TYPE>Grain</TYPE><AMOUNT>9.0718474</AMOUNT></FERMENTABLE></FERMENTABLES>
<MASH><NAME>Single Infusion</NAME><GRAIN_TEMP>22.2222222</GRAIN_TEMP><TUN_TEMP>22.2222222</TUN_TEMP>
<EQUIP_ADJUST>{adjust}</EQUIP_ADJUST><TUN_WEIGHT>528.0</TUN_WEIGHT><TUN_SPECIFIC_HEAT>0.12</TUN_SPECIFIC_HEAT>
<DISPLAY_TUN_WEIGHT>33 lbs</DISPLAY_TUN_WEIGHT><MASH_STEPS>
<MASH_STEP><NAME>Saccharification</NAME><TYPE>Infusion</TYPE><STEP_TEMP>{step_temp}</STEP_TEMP>
<INFUSE_TEMP>{infuse_temp}</INFUSE_TEMP><INFUSE_AMOUNT>{amount}</INFUSE_AMOUNT>
<DISPLAY_INFUSE_AMT>{shown}</DISPLAY_INFUSE_AMT></MASH_STEP>
<MASH_STEP><NAME>Mash Out</NAME><TYPE>Temperature</TYPE><STEP_TEMP>75.5555556</STEP_TEMP>
<INFUSE_AMOUNT>0.0</INFUSE_AMOUNT></MASH_STEP></MASH_STEPS></MASH></RECIPE></RECIPES>""".format(
        infuse_temp=infuse_temp, step_temp=step_temp, shown=shown, amount=amount, adjust=adjust)).find("RECIPE")


# ------------------------------------------------------------------------------------------------- the import

def test_the_import_keeps_the_first_rest_and_adjust_temp_for_equip():
    upload = UploadController.__new__(UploadController)
    found = upload.getMashNumbers(beerxml())
    assert found["rest_c"] == pytest.approx(66.6667, abs=1e-4) and found["equip_adjust"] is True
    assert round(found["infuse_temp_c"] * 1.8 + 32, 1) == 157.7
    v6 = upload.getMashNumbers(beerxml(infuse_temp="165.3 F", shown="28.04 qt", amount="23.65896", adjust="FALSE"))
    assert v6["rest_c"] == pytest.approx(66.6667, abs=1e-4) and v6["equip_adjust"] is False


# ------------------------------------------------------------------------------------------- what MashIn heats to

@pytest.mark.parametrize("numbers, rest, unit, strike, expected", [
    (V6C, 152.0, "F", 157.7, 155.7),
    (V6, 152.0, "F", 165.3, 162.0),
    (V6C, 66.6667, "C", 69.8, 68.7),
], ids=["v6c", "v6", "v6c-celsius"])
def test_mashin_heats_to_beersmiths_strike_less_the_steel_it_counted(numbers, rest, unit, strike, expected):
    assert bs.dough_in_temperature(strike, numbers, unit, rest) == expected


def test_the_steel_counted_is_worked_out_from_beersmiths_figures_not_the_whole_tun():
    whole = V6C["tun_kg"] * V6C["tun_cal_g_c"]
    assert bs.steel_counted(V6C) == pytest.approx(whole, abs=1e-6)       # 19.5 gal fills the tun: all of it
    assert bs.steel_counted(V6) == pytest.approx(0.964, abs=0.002)       # 7 gal: 54 % of it
    assert bs.dough_in_temperature(165.3, V6, "F", 152.0) > (26.535737 * 165.3 + whole * 72.0) / (26.535737 + whole)


def test_with_adjust_temp_for_equip_off_mashin_heats_to_the_strike():
    off = dict(V6C, equip_adjust=False)
    assert bs.steel_counted(off) == 0.0
    assert bs.dough_in_temperature(157.7, off, "F", 152.0) == 157.7


def test_a_degree_on_the_strike_moves_the_pre_heat_by_the_waters_share():
    w, cs = V6C["infuse_l"], bs.steel_counted(V6C)
    moved = bs.dough_in_temperature(167.7, V6C, "F", 152.0) - bs.dough_in_temperature(157.7, V6C, "F", 152.0)
    assert moved == pytest.approx(10 * w / (w + cs), abs=0.1)


def test_the_brewers_changed_mash_water_keeps_beersmiths_own_for_the_steel():
    typed = dict(V6C, infuse_l=60.0, typed=["water"], beersmith={"infuse_l": V6C["infuse_l"]})
    assert bs.steel_counted(typed) == pytest.approx(bs.steel_counted(V6C))
    assert bs.dough_in_temperature(157.7, typed, "F", 152.0) == round((60 * 157.7 + bs.steel_counted(V6C) * 72.0)
                                                                      / (60 + bs.steel_counted(V6C)), 1)


@pytest.mark.parametrize("strike, rest, unit, expected", [
    (140.0, 152.0, "F", 152.0),          # never under the rest
    (200.0, 140.0, "F", 167.0),          # at most 27 F over it
    (200.0, 160.0, "F", 179.6),          # never over 179.6 F
    (95.0, 60.0, "C", 75.0),             # 15 C over the rest
    (95.0, 70.0, "C", 82.0),             # 82 C
])
def test_the_pre_heat_keeps_within_mashins_limits(strike, rest, unit, expected):
    numbers = dict(V6C, equip_adjust=False)
    assert bs.dough_in_temperature(strike, numbers, unit, rest) == expected


@pytest.mark.parametrize("numbers, strike, rest, water, tun", [
    ({k: v for k, v in V6C.items() if k != "rest_c"}, 157.7, 152.0, None, None),    # imported before rest_c
    ({k: v for k, v in V6C.items() if k != "grain_temp_c"}, 157.7, 152.0, None, None),
    ({k: v for k, v in V6C.items() if k != "infuse_l"}, 157.7, 152.0, None, None),
    (V6C, None, 152.0, None, None),
    (V6C, 157.7, None, None, None),
    ({"grain_kg": 9.07, "infuse_l": 30.0}, 140.0, 126.0, None, None),             # no BeerSmith strike, no tun
], ids=["no-rest_c", "no-grain-temp", "no-water", "no-strike", "no-rest", "no-tun"])
def test_a_figure_missing_gives_no_pre_heat(numbers, strike, rest, water, tun):
    assert bs.dough_in_temperature(strike, numbers, "F", rest, water_l=water, tun=tun) is None


def test_without_beersmiths_strike_a_typed_one_less_the_whole_tun():
    numbers = {"grain_kg": 9.07, "infuse_l": 30.0, "lacks": ["infuse_temp"]}
    assert bs.dough_in_temperature(140.0, numbers, "F", 126.0, tun=(1.8, 68.0)) == round(
        (30 * 140 + 1.8 * 68) / 31.8, 1)


# ------------------------------------------------------------------------------------------------ the dough-in MashIn

class FakeKettle:
    def __init__(self):
        self.id, self.name, self.target_temp, self.instance = "mt", "Mash Tun", 0, None


class FakeTimer:
    is_running = False

    async def stop(self):
        pass


def mashin(props, preheat=None, unit="F", grain_temp=60.0):
    step = object.__new__(MashInStep)
    kettle = FakeKettle()
    notes = []
    step.cbpi = types.SimpleNamespace(
        notify=lambda title, message, type=None, action=None: notes.append(message),
        kettle=types.SimpleNamespace(find_by_id=lambda _id: kettle, set_target_temp=mock.AsyncMock()),
        step=types.SimpleNamespace(save=mock.AsyncMock()))
    if preheat is not None:
        step.cbpi.strike = types.SimpleNamespace(preheat_for=mock.Mock(return_value=preheat))
    step.id, step.name, step.props = "mi", "MashIn", dict(props)
    step.notes = notes
    step.get_sensor_value = lambda _id: {"value": grain_temp}
    step.get_config_value = lambda key, default: unit if key == "TEMP_UNIT" else default
    step.timer = FakeTimer()
    step.summary, step.running = "", True
    step._stall_prompt, step._stall_anchor_value, step._stall_anchor_at, step._stall_reported = None, None, 0.0, False
    step.setAutoMode = mock.AsyncMock()
    step.push_update = mock.AsyncMock()
    return step


MASHIN = {"Temp": 152, "Kettle": "mt", "Sensor": "mt-probe", "AutoMode": "Yes"}


def test_with_beersmiths_figures_mashin_heats_to_its_pre_heat_then_the_rest_once_it_asks():
    step = mashin(MASHIN, preheat=155.7)
    asyncio.run(step.on_start())
    assert step.kettle.target_temp == 155.7 and step.strike_temp == 155.7
    step.cbpi.strike.preheat_for.assert_called_with("mi")
    asyncio.run(step.on_timer_done(step.timer))
    assert step.kettle.target_temp == 152


def test_without_a_pre_heat_mashin_heats_to_its_temp():
    step = mashin(MASHIN)
    asyncio.run(step.on_start())
    assert step.kettle.target_temp == 152


def test_with_beersmiths_figures_the_grain_sensor_adds_no_second_rise():
    own = dict(MASHIN, Grain_Kg=9.07, Mash_Water=73.85, Grain_Sensor="room")
    alone = mashin(own)
    asyncio.run(alone.on_start())
    assert alone.kettle.target_temp > 152.5 and alone.notes        # its own strike, as before
    step = mashin(own, preheat=155.7)
    asyncio.run(step.on_start())
    assert step.kettle.target_temp == 155.7 and not step.notes


def test_a_pre_heat_past_mashins_limits_is_held_to_them():
    step = mashin(MASHIN, preheat=200.0)
    asyncio.run(step.on_start())
    assert step.kettle.target_temp == 179.0


def test_set_preheat_while_heating_moves_only_the_target():
    step = mashin(MASHIN, preheat=155.7)
    asyncio.run(step.on_start())
    assert asyncio.run(step.set_preheat(156.6)) is True
    assert step.strike_temp == 156.6 and step.props["Temp"] == 152 and step.rest_temp == 152
    step.cbpi.kettle.set_target_temp.assert_awaited_with("mt", 156.6)
    step.setAutoMode.assert_awaited_once_with(True)            # switched on at the start, by on_start only


def test_set_preheat_does_nothing_once_the_malt_is_asked_for():
    step = mashin(MASHIN, preheat=155.7)
    asyncio.run(step.on_start())
    asyncio.run(step.on_timer_done(step.timer))
    assert asyncio.run(step.set_preheat(158.0)) is False
    assert step.kettle.target_temp == 152


def test_a_pre_heat_stalled_short_says_so_and_switches_nothing(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("cbpi.extension.mashstep.clock.now", lambda: now[0])
    step = mashin(MASHIN, preheat=155.7)
    asyncio.run(step.on_start())
    calls = (step.cbpi.kettle.set_target_temp.await_count, step.setAutoMode.await_count)
    for minute in range(11):
        now[0] = 1000.0 + 60 * minute
        step._watch_preheat(155.3 + 0.001 * minute, 155.7)
    assert step.preheat_stall == {"short": 0.4, "min": 10}
    assert (step.cbpi.kettle.set_target_temp.await_count, step.setAutoMode.await_count) == calls
    assert step.kettle.target_temp == 155.7 and not step.notes
    step._watch_preheat(155.5, 155.7)                          # climbing again
    assert step.preheat_stall is None
    step._watch_preheat(153.0, 155.7)                          # further off: the stall watch's, not this
    assert step.preheat_stall is None


# --------------------------------------------------------------------------------------------- the strike in use

class Folder:
    def __init__(self, root):
        self.root = root

    def get_file_path(self, name):
        return str(self.root / name)

    def get_recipe_file_by_id(self, name):
        return str(self.root / "recipes" / "{}.yaml".format(name))


class Herms:
    OUTER_LOOP_RATIO = 4

    @staticmethod
    def blank_defaults(unit):
        return {}


HERMS_MODULE = types.ModuleType("strike_bs_test_pid_herms")
HERMS_MODULE.PIDArduino = object
Herms.__module__ = HERMS_MODULE.__name__


class BrewStart:
    HEATING_PROP = "_brewstart_heating"

    def __init__(self, props):
        self.props, self.heating, self.calls = props, False, []

    async def change_target(self, temp):
        self.calls.append(temp)
        self.props["Ready_Temp"] = temp
        return False

    def forget_wait(self):
        return False


def kettle(kettle_id, name, logic, sensor, heater, props=None):
    return types.SimpleNamespace(id=kettle_id, name=name, type=logic, sensor=sensor, heater=heater, agitator=None,
                                 props=Props(props or {}), target_temp=0, instance=None)


def brew(tmp_path, monkeypatch, numbers=V6C, ready=157.7, bs_status=StepState.ACTIVE, mi_status=StepState.INITIAL,
         mi_props=None, room=None):
    """v6c's brew steps on a HERMS: BrewStart on the HLT, MashIn on the mash tun, the rest. With `room`, the boil
    kettle's probe reads it, so the forecast has every number (the worker is a stand-in that is never sent to)."""
    monkeypatch.setitem(sys.modules, HERMS_MODULE.__name__, HERMS_MODULE)
    kettles = [kettle("hlt", "HLT", "Hysteresis", "hlt-probe", "hlt-heater"),
               kettle("mt", "Mash Tun", "PID_HERMS", "mt-probe", "hlt-heater", {"HLT_Kettle": "hlt"}),
               kettle("bk", "Boil Kettle", "BoilPower", "bk-probe" if room else None, "bk-heater")]
    probe = types.SimpleNamespace(instance=object(), props=Props({}))
    props = Props({"Timing": "Start at", "Heat_Kettle": "hlt", "Ready_Temp": ready, "Volume": 22})
    start = Step("bs", "BrewStart", type="BrewStart", status=bs_status, props=props)
    start.instance = BrewStart(props)
    mi = Step("mi", "MashIn", type="MashInStep", status=mi_status,
              props=Props(dict({"Kettle": "mt", "Temp": 152}, **(mi_props or {}))))
    mi.instance = types.SimpleNamespace(set_preheat=mock.AsyncMock(return_value=True), _waiting_for_malt=False,
                                        preheat_stall=None)
    rest = Step("r1", "Saccharification", type="MashStep", status=StepState.INITIAL,
                props=Props({"Kettle": "mt", "Temp": 152}))
    cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: {"TEMP_UNIT": "F"}.get(key, default)),
        config_folder=Folder(tmp_path),
        step=types.SimpleNamespace(profile=[start, mi, rest], save=mock.AsyncMock(),
                                   basic_data={"name": "Craftbeerpi Test Recipe v6c", "mash_numbers": dict(numbers)}),
        kettle=types.SimpleNamespace(data=kettles, set_target_temp=mock.AsyncMock(),
                                     types={"PID_HERMS": {"class": Herms, "properties": [{"label": "HLT_Sensor"}]}}),
        sensor=types.SimpleNamespace(
            find_by_id=lambda sensor_id: probe if room and sensor_id == "bk-probe" else None,
            get_sensor_value=lambda sensor_id: {"value": room, "age": 1.0, "max_age": 60.0} if room else None))
    helper = StrikeController(cbpi, watch=False)
    helper.client, helper.fitter = mock.Mock(), mock.Mock()
    return helper


def strike_of(helper):
    return helper.get_state(helper.update())["strike"]


def send(helper, **body):
    return asyncio.run(helper.strike(dict(body, steps=["bs", "mi", "r1"])))


def refused(helper, **body):
    with pytest.raises(StrikeRefused) as caught:
        send(helper, **body)
    return caught.value


def test_the_strike_in_use_is_beersmiths_and_mashin_heats_to_less(tmp_path, monkeypatch):
    found = strike_of(brew(tmp_path, monkeypatch))
    assert (found["value"], found["from"], found["beersmith"], found["mashin"]) == (157.7, "beersmith", 157.7, 155.7)
    assert found["asked"] is False and found["stall"] is None and found["stale"] is False
    assert found["range"] == [152.0, 179.0]
    assert found["balance"]["steel"] == pytest.approx(1.796, abs=0.001) and found["balance"]["tun"] == 72.0
    helper = brew(tmp_path, monkeypatch)
    assert helper.strike_in_use() == {"value": 157.7, "from": "beersmith"}
    assert helper.preheat_for("mi") == 155.7 and helper.preheat_for("r1") is None


@pytest.mark.parametrize("numbers, ready, label", [
    (V6C, 159.0, "yours"),
    ({"grain_kg": 9.07, "infuse_l": 30.0, "lacks": ["infuse_temp"]}, 152.0, "rest"),
    ({"grain_kg": 9.07, "infuse_l": 30.0, "lacks": ["infuse_temp"]}, 156.0, "yours"),
    (V6C, 152.0, "yours"),                                    # an old book BrewStart at the rest
], ids=["yours", "rest", "yours-without-beersmith", "rest-with-beersmith"])
def test_where_the_strike_comes_from(tmp_path, monkeypatch, numbers, ready, label):
    assert strike_of(brew(tmp_path, monkeypatch, numbers=numbers, ready=ready))["from"] == label


def test_without_beersmiths_figures_mashin_heats_to_its_temp(tmp_path, monkeypatch):
    found = strike_of(brew(tmp_path, monkeypatch, numbers={"grain_kg": 9.07, "infuse_l": 30.0, "lacks": []},
                           ready=152.0))
    assert found["mashin"] is None and found["beersmith"] is None


def test_use_written_is_the_checks(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch)
    look = helper.update()
    asyncio.run(helper._apply_strike(look, "use", 159.4))
    assert strike_of(helper)["from"] == "check"
    send(helper, do="set", temp=159.4)
    assert strike_of(helper)["from"] == "yours"


def test_set_and_beersmith_write_brewstarts_ready_temp_before_the_move(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch)
    out = send(helper, do="set", temp=159.0)
    start = helper.cbpi.step.profile[0]
    assert start.props["Ready_Temp"] == 159.0 and start.instance.calls == [159.0]
    assert out["applied"]["to"] == 159.0 and out["applied"]["from"] == 157.7
    assert out["applied"]["mashin"] == out["strike"]["strike"]["mashin"] > 155.7
    assert out["strike"]["strike"]["from"] == "yours"
    out = send(helper, do="beersmith")
    assert start.props["Ready_Temp"] == 157.7 and out["strike"]["strike"]["from"] == "beersmith"
    assert out["applied"]["mashin"] == 155.7


def test_set_outside_the_range_is_refused_and_changes_nothing(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch)
    for temp in (150.0, 181.0):
        out = refused(helper, do="set", temp=temp)
        assert (out.status, out.error, out.more.get("low"), out.more.get("high")) == (400, "out_of_range", 152.0,
                                                                                       179.0)
    assert helper.cbpi.step.profile[0].props["Ready_Temp"] == 157.7


def test_after_the_move_a_new_strike_moves_only_mashins_pre_heat(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch, bs_status=StepState.DONE, mi_status=StepState.ACTIVE)
    out = send(helper, do="set", temp=160.0)
    start, mi = helper.cbpi.step.profile[0], helper.cbpi.step.profile[1]
    assert start.props["Ready_Temp"] == 157.7 and start.instance.calls == []
    helper.cbpi.kettle.set_target_temp.assert_not_awaited()
    pre = bs.dough_in_temperature(160.0, V6C, "F", 152.0)
    mi.instance.set_preheat.assert_awaited_once_with(pre)
    assert out["applied"]["changed"] == ["mashin"] and out["applied"]["mashin"] == pre
    assert out["strike"]["strike"]["value"] == 160.0 and out["strike"]["strike"]["from"] == "yours"
    assert helper.preheat_for("mi") == pre
    send(helper, do="beersmith")
    mi.instance.set_preheat.assert_awaited_with(155.7)
    assert strike_of(helper)["from"] == "beersmith"


def test_once_the_malt_is_asked_for_the_strike_is_refused(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch, bs_status=StepState.DONE, mi_status=StepState.ACTIVE,
                  mi_props={"_waiting_for_malt": True})
    assert strike_of(helper)["asked"] is True
    for body in (dict(do="set", temp=160.0), dict(do="beersmith")):
        out = refused(helper, **body)
        assert (out.status, out.error) == (409, "asked") and out.more["strike"]["strike"]["asked"] is True
    helper.cbpi.step.profile[1].instance.set_preheat.assert_not_awaited()


def test_a_stalled_pre_heat_is_in_the_state(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch, bs_status=StepState.DONE, mi_status=StepState.ACTIVE)
    helper.cbpi.step.profile[1].instance.preheat_stall = {"short": 0.4, "min": 12}
    assert strike_of(helper)["stall"] == {"short": 0.4, "min": 12}


def test_the_log_says_the_mash_water_and_grain_weight_in_the_cards_words(tmp_path, monkeypatch, caplog):
    # Brew Day's card changes BeerSmith's figures with edit, as the recipe editor does: the LOG line says what changed,
    # never "in the recipe editor".
    helper = brew(tmp_path, monkeypatch)
    ids = ["bs", "mi", "r1"]
    with caplog.at_level("INFO", logger="cbpi.controller.strike_controller"):
        asyncio.run(helper.set_recipe({"strike_vol": 7.0, "edit": True, "steps": ids}))
        asyncio.run(helper.set_recipe({"back": ["strike_vol"], "steps": ids}))
        asyncio.run(helper.set_recipe({"grain": 18.0, "edit": True, "steps": ids}))
    said = [r.getMessage() for r in caplog.records if r.getMessage().startswith(("Mash water", "Grain weight"))]
    assert said == ["Mash water set to 7.00 gal, was 19.51 gal", "Mash water back to BeerSmith's 19.51 gal",
                    "Grain weight set to 18.0 lb, was 20.0 lb"]


def answered(helper, level, lands, strike="157.7"):
    """The worker's answer for the numbers now, as the helper keeps it: USE 156.1 F, the check's strike `level`, and
    the central case landing at `lands` with the strike in use."""
    look = helper.update()
    forecast = {"strike": float(strike), "searched": False, "peak": 152.1, "reads": 156.0, "lo": [], "hi": [],
                "lands": lands}
    helper.job = None
    helper.answer = dict(plan="p1", at=0.0, took=1.0, runs=10, rule="no worse", use=156.1, use_f=156.1, far=False,
                         rest_peak=None, forecasts={strike: forecast}, levers=None, levers_status="ready", unit="F",
                         level=level, numbers=dict(look.numbers, key=look.key), _job={}, _cases={})
    return helper


@pytest.mark.parametrize("lands, note, use", [(150.4, True, 159.4), (151.0, False, None), (153.2, True, 159.4)],
                         ids=["low", "within", "high"])
def test_the_check_says_so_only_over_one_degree(tmp_path, monkeypatch, lands, note, use):
    helper = answered(brew(tmp_path, monkeypatch, room=72.0), 159.4, lands)
    check = helper.get_state(helper.update())["check"]
    assert check == {"landing": lands, "note": note, "within": 1.0, "use": use}


def test_the_checks_strike_is_taken_by_use_and_tagged_check(tmp_path, monkeypatch):
    helper = answered(brew(tmp_path, monkeypatch, room=72.0), 159.4, 150.4)
    out = send(helper, do="use", temp=159.4, plan="p1")
    assert helper.cbpi.step.profile[0].props["Ready_Temp"] == 159.4 and out["strike"]["strike"]["from"] == "check"


def test_the_check_offers_nothing_after_the_move(tmp_path, monkeypatch):
    helper = brew(tmp_path, monkeypatch, bs_status=StepState.DONE, mi_status=StepState.ACTIVE, room=72.0)
    check = answered(helper, 159.4, 150.4).get_state(helper.update())["check"]
    assert check["note"] is True and check["use"] is None


# ------------------------------------------------------------------------------------------------ UPDATE import

def test_update_moves_only_a_ready_temp_still_at_beersmiths_old_strike(tmp_path):
    (tmp_path / "recipes").mkdir()
    steps = [{"name": "BrewStart", "type": "BrewStart", "status": "I", "props": {"Ready_Temp": 157.7}},
             {"name": "Mine", "type": "BrewStart", "status": "I", "props": {"Ready_Temp": 159.0}},
             {"name": "MashIn", "type": "MashInStep", "status": "I", "props": {"Temp": 157.7}}]
    name = "Craftbeerpi Test Recipe v6c"
    (tmp_path / "recipes" / "mine.yaml").write_text(yaml.dump(
        {"basic": {"name": name, "mash_numbers": dict(V6C)}, "steps": steps}))
    cbpi = mock.Mock()
    cbpi.config_folder = Folder(tmp_path)
    cbpi.config.get = lambda key, default=None: "F" if key == "TEMP_UNIT" else default
    new = dict(V6C, infuse_temp_c=70.0)                       # 158.0 F
    asyncio.run(RecipeController(cbpi).update_from_import(
        "mine", {"basic": {"name": name, "mash_numbers": new}, "steps": []}, "BeerXML"))
    kept = yaml.safe_load((tmp_path / "recipes" / "mine.yaml").read_text())["steps"]
    assert [s["props"].get("Ready_Temp", s["props"].get("Temp")) for s in kept] == [158.0, 159.0, 157.7]


def test_update_moves_a_loaded_brewstart_not_yet_begun(tmp_path):
    book = RecipeController.__new__(RecipeController)
    book.cbpi = types.SimpleNamespace(config=types.SimpleNamespace(get=lambda key, default=None: "F"))
    waiting = Step("a", "BrewStart", type="BrewStart", status=StepState.INITIAL, props=Props({"Ready_Temp": 157.7}))
    heating = Step("b", "BrewStart", type="BrewStart", status=StepState.ACTIVE, props=Props({"Ready_Temp": 157.7}))
    assert book.strike_follows_update([waiting, heating], V6C, dict(V6C, infuse_temp_c=70.0)) == 1
    assert (waiting.props["Ready_Temp"], heating.props["Ready_Temp"]) == (158.0, 157.7)


# ------------------------------------------------------------------------------------------------------ the model

class PID:
    """A proportional controller in PIDArduino's shape, enough for the replay's flow."""

    def __init__(self, sample, p, i, d, low, high, getTimeMs=None):
        self.p, self.high, self._iTerm = p, high, 0.0

    def calc(self, value, target):
        return max(0.0, min(self.high, self.p * (target - value)))

    def set_output_max(self, high):
        self.high = high


def rig():
    herms = model.Herms(pid=PID, sample_time=2, p=20.0, i=0.0, d=0.0, hlt_p=60.0, hlt_i=0.0, hlt_d=0.0, delta_f=8.0,
                        mashin_delta_f=8.0, integral_carry_s=-1)
    return model.Rig(plant=model.Plant(), herms=herms)


def v6c_brew(pre_heat=True, probe="coil"):
    return model.Brew(grain_kg=9.07184, strike_gal=73.853384 / model.L_PER_GAL, rest_f=152.0, hlt_total_gal=22.0,
                      tun_kg=14.968536, tun_cal_g_c=0.12, garage_f=72.0, probe=probe,
                      steel_counted_kg=bs.steel_counted(V6C) if pre_heat else None, steel_from_f=72.0)


def test_the_models_mashin_asks_for_the_malt_at_its_pre_heat():
    # The tun's probe, the whole tun's steel warmed and 2 F lost in the hoses: the water lands under the pre-heat.
    box = model.ranges(v6c_brew(probe="tun"), rig().plant)
    params = dict(model.central(box), tun_share=1.0, transfer_f=2.0)
    assert model.mashin_target_f(v6c_brew(), 157.7) == pytest.approx(bs.dough_in_temperature(157.7, V6C, "F", 152.0),
                                                                     abs=0.05)
    at_rest = model.simulate(v6c_brew(False, probe="tun"), 157.7, params, rig())
    pre_heat = model.simulate(v6c_brew(probe="tun"), 157.7, params, rig())
    assert pre_heat.probe_doughin_max_f >= model.mashin_target_f(v6c_brew(), 157.7) - 0.5 > at_rest.probe_doughin_max_f
    assert pre_heat.ask_s > at_rest.ask_s
    assert pre_heat.landing_f > at_rest.landing_f + 1.0


def test_the_checks_strike_lands_the_central_case_on_the_rest():
    brew_, r = v6c_brew(), rig()
    box = model.ranges(brew_, r.plant)
    level = model.level_strike(brew_, r, box)["strike_f"]
    assert 152.0 < level <= 179.0
    assert model.simulate(brew_, level, model.central(box), r).landing_f == pytest.approx(152.0, abs=0.15)
    fc = model.forecast(brew_, level, r, {"central": model.central(box)})
    assert fc.central_f == pytest.approx(152.0, abs=0.15)

def test_a_pre_heat_the_herms_cant_reach_is_left_out_not_a_failure():
    # The tun's probe, the whole tun warmed, 2 F lost in the hoses and a coil that passes next to nothing: MashIn
    # would wait for its pre-heat for ever in that case. A forecast leaves the case out, and the planner takes the
    # strike as past any line in it, rather than failing.
    brew_, r = v6c_brew(probe="tun"), rig()
    box = model.ranges(brew_, r.plant)
    stuck = dict(model.central(box), tun_share=1.0, transfer_f=2.0, coil_ua=1.0)
    with pytest.raises(model.NeverAsked):
        model.simulate(brew_, 157.7, stuck, r)
    fc = model.forecast(brew_, 157.7, r, {"central": model.central(box), "stuck": stuck})
    assert fc.sims == 1 and fc.central_f is not None
    judge = model.Judge(brew_, r)
    assert judge.run(157.7, stuck) == (None, math.inf) and not judge.passes(157.7, stuck)