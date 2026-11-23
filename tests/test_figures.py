"""Figures from past brews (cbpi.figures): the brews found in logs, each figure's state, grain absorption, and the
steps' numbers in the logs (BUILDERS.md C1, C3)."""

import datetime
import os
import shutil
import types
import uuid

import pytest

from cbpi.api.dataclasses import Step
from cbpi.api.step import StepState
from cbpi.controller.step_controller import StepController
from cbpi.figures import analysis, ledger

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = 1_790_000_000.0
LITRES, WATTS = 40.0, 6500.0
PARAMS = {"unit": "F", "hlt_sim": {"litres": LITRES, "watts": WATTS}, "vessels": {
    "hlt": {"probe": "H", "kettle": "hk", "element": "he", "pump": None},
    "mash": {"probe": "M", "kettle": "mk", "element": "he", "pump": "wp"},
    "boil": {"probe": "B", "kettle": "bk", "element": "be"}}}


@pytest.fixture
def folder():
    path = os.path.join(HERE, "temp-figures-%s" % uuid.uuid4().hex[:8])
    os.makedirs(path)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def write(folder, name, rows):
    with open(os.path.join(folder, "sensor_%s.log" % name), "a", encoding="utf-8") as f:
        for t, v in rows:
            f.write("%s,%s\n" % (datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], v))


def series(start, end, value, step=10.0):
    rows, t = [], start
    while t <= end:
        rows.append((t, round(value(t), 3)))
        t += step
    return rows


def hlt_heat(folder, start, eff, pump=False):
    """The HLT heated from 60 F to 160 F by its element from start+600 s, at `eff` of 6500 W into 40 L; the mash
    following it when the wort pump runs."""
    rate = eff * WATTS / (LITRES * 0.995 * 4181.0) * 1.8
    on = start + 600.0

    def h(t):
        return 60.0 if t < on else min(160.0, 60.0 + (t - on) * rate)

    write(folder, "H", series(start, start + 3 * 3600, h))
    write(folder, "M", series(start, start + 3 * 3600, (lambda t: 60.0 + 0.8 * (h(t) - 60.0)) if pump
                              else (lambda t: 60.0)))
    write(folder, "B", series(start, start + 3 * 3600, lambda t: 60.0))
    write(folder, "he.out", [(start, 0), (on, 100)])
    write(folder, "hk.target", [(start, 160.0)])
    write(folder, "wp.out", [(start, 100 if pump else 0)])
    return on


def test_hlt_heated_alone_gives_brewstarts_efficiency(folder):
    on = hlt_heat(folder, BASE, 0.85)
    found = analysis.analyse(folder, PARAMS)
    assert len(found["brews"]) == 1
    cell = found["brews"][0]["cells"]["hlt"]
    assert cell["eff"] == pytest.approx(0.85, abs=0.004)
    assert cell["t0"] == pytest.approx(on, abs=1)
    assert cell["from"] == 60.0 and 159.5 <= cell["to"] <= 160.0
    assert cell["planned_by"] == "worked_out"
    assert cell["planned"] == pytest.approx(cell["minutes"] * 0.85 / 0.90, abs=0.2)


def test_hlt_heated_with_the_wort_pump_is_no_figure(folder):
    hlt_heat(folder, BASE, 0.85, pump=True)
    found = analysis.analyse(folder, PARAMS)
    cells = [b["cells"]["hlt"] for b in found["brews"]]
    assert cells == [{"none": "pump_on"}] or cells == []


def test_mash_held_under_120_with_the_hlt_heating_it_is_no_brew(folder):
    def k(t):
        return min(1.0, max(0.0, (t - BASE - 600.0) / 1800.0))

    write(folder, "H", series(BASE, BASE + 4 * 3600, lambda t: 60.0 + 55.0 * k(t)))
    write(folder, "M", series(BASE, BASE + 4 * 3600, lambda t: 60.0 + 50.0 * k(t)))
    write(folder, "B", series(BASE, BASE + 4 * 3600, lambda t: 60.0))
    assert analysis.analyse(folder, PARAMS)["brews"] == []


def test_a_vessel_that_starts_hot_and_cools_is_no_brew(folder):
    write(folder, "H", series(BASE, BASE + 4 * 3600, lambda t: max(70.0, 168.0 - (t - BASE) / 60.0)))
    write(folder, "M", series(BASE, BASE + 4 * 3600, lambda t: max(70.0, 152.0 - (t - BASE) / 60.0)))
    write(folder, "B", series(BASE, BASE + 4 * 3600, lambda t: max(70.0, 212.0 - (t - BASE) / 60.0)))
    assert analysis.analyse(folder, PARAMS)["brews"] == []


def mash_rest(folder, start, over, rest_min=40.0):
    """The mash heated through the coil to a rest at 152 F, coming `over` F over, held, then left for 168 F."""
    arrive = start + 600.0 + (152.0 + over - 60.0) / 1.5 * 60.0

    def m(t):
        if t < start + 600.0:
            return 60.0
        if t < arrive:
            return 60.0 + (t - start - 600.0) / 60.0 * 1.5
        if t < arrive + 180.0:
            return 152.0 + over * (1 - (t - arrive) / 180.0)
        if t < arrive + rest_min * 60.0:
            return 152.0
        return min(168.0, 152.0 + (t - arrive - rest_min * 60.0) / 60.0)

    end = arrive + rest_min * 60.0 + 3600.0
    write(folder, "M", series(start, end, m))
    write(folder, "H", series(start, end, lambda t: m(t) + 5.0))
    write(folder, "B", series(start, end, lambda t: 60.0))
    return arrive


def test_a_rest_how_far_it_came_over_and_held(folder):
    mash_rest(folder, BASE, 0.5)
    brew = analysis.analyse(folder, PARAMS)["brews"][0]
    rests = brew["cells"]["mash"]["rests"]
    assert [r["target"] for r in rests] == [152.0, 168.0]
    assert rests[0]["over"] == 0.5
    assert 0 < rests[0]["held"] <= 0.5
    assert brew["cells"]["mash"]["steps_up"][0]["rate"] == pytest.approx(1.0, abs=0.02)
    assert brew["cells"]["hlt"] == {"none": "pump_on"}


def test_brews_less_than_three_hours_apart_are_one(folder):
    mash_rest(folder, BASE, 0.2)
    mash_rest(folder, BASE + 4 * 3600 + 1800, 0.2)
    assert len(analysis.analyse(folder, PARAMS)["brews"]) == 1


def test_brews_more_than_three_hours_apart_are_two(folder):
    mash_rest(folder, BASE, 0.2)
    mash_rest(folder, BASE + 10 * 3600, 0.2)
    assert len(analysis.analyse(folder, PARAMS)["brews"]) == 2


def test_the_boil_its_rate_or_where_it_stopped(folder):
    write(folder, "H", series(BASE, BASE + 4 * 3600, lambda t: 60.0))
    write(folder, "M", series(BASE, BASE + 4 * 3600, lambda t: 60.0))
    write(folder, "B", series(BASE, BASE + 4 * 3600,
                              lambda t: 60.0 if t < BASE + 600 else min(212.0, 60.0 + (t - BASE - 600) / 60.0 * 2.5)))
    cell = analysis.analyse(folder, PARAMS)["brews"][0]["cells"]["boil"]
    assert cell["rate"] == 2.5 and cell["from"] == 60.0
    assert cell["minutes"] == pytest.approx((211.5 - 60.0) / 2.5, abs=0.3)


def brews(**cells):
    """Brews with one figure's values: {id: cell}."""
    return [{"id": k, "cells": {"hlt": {"eff": v}, "boil": {"rate": v * 3}, "grain": {"per": v / 8}}}
            for k, v in cells.items()]


def settings(hlt=None, registered=True, grain=0.12):
    return {"hlt": {"registered": registered, "value": hlt}, "grain": {"value": grain},
            "strike": {"available": True, "guess": {"low": 0.45, "central": 0.75, "high": 1.0, "measured": None}},
            "mash": None}


def test_two_brews_that_agree_offer_use():
    figs = ledger.all_figures(brews(a=0.84, b=0.86), {}, "F", settings())
    assert figs["hlt"]["state"] == "use"
    assert figs["hlt"]["use"] == {"value": 0.85, "brews": ["a", "b"]}
    assert figs["boil"]["state"] == "info" and figs["boil"]["use"] is None


def test_one_brew_none_and_disagree():
    assert ledger.all_figures(brews(a=0.84), {}, "F", settings())["hlt"]["state"] == "one"
    assert ledger.all_figures([], {}, "F", settings())["hlt"]["state"] == "none"
    figs = ledger.all_figures(brews(a=0.80, b=0.86), {}, "F", settings())
    assert figs["hlt"]["state"] == "disagree" and figs["hlt"]["use"] is None


def test_left_out_brews_dont_count():
    figs = ledger.all_figures(brews(a=0.84, b=0.86, c=0.95), {"out": {"hlt": ["c"]}}, "F", settings())
    assert figs["hlt"]["state"] == "use" and figs["hlt"]["n"] == 2
    assert [v["out"] for v in figs["hlt"]["values"]] == [False, False, True]


def test_in_use_only_while_the_setting_holds_what_use_set():
    store = {"in_use": {"hlt": {"value": 0.85, "since": 5.0, "brews": ["a", "b"], "was": None}}}
    figs = ledger.all_figures(brews(a=0.84, b=0.86), store, "F", settings(hlt=85))
    assert figs["hlt"]["state"] == "in_use"
    assert figs["hlt"]["set"] == {"setting": "BREWSTART_EFFICIENCY", "value": 0.85, "built_in": False, "since": 5.0,
                                  "was": None}
    by_hand = ledger.all_figures(brews(a=0.84, b=0.86), store, "F", settings(hlt=88))
    assert by_hand["hlt"]["state"] == "use" and by_hand["hlt"]["set"]["since"] is None


def test_brewstart_without_the_setting():
    figs = ledger.all_figures(brews(a=0.84, b=0.86), {}, "F", settings(registered=False))
    assert figs["hlt"]["why_not"] == "brewstart_old"


def test_efficiency_read_as_brewstart_reads_it():
    assert ledger.efficiency_fraction(85) == (0.85, False)
    assert ledger.efficiency_fraction("0.85") == (0.85, False)
    assert ledger.efficiency_fraction("") == (0.9, True)
    assert ledger.efficiency_fraction(150) == (0.9, True)


def test_mash_caution_when_a_rest_came_over():
    rest = {"target": 152.0, "t0": 1, "t1": 2, "minutes": 60, "held": 0.2, "over": 0.9, "last_degree": 3}
    calm = dict(rest, over=0.3)
    figs = ledger.all_figures([{"id": "a", "cells": {"mash": {"rests": [rest], "steps_up": []}}},
                               {"id": "b", "cells": {"mash": {"rests": [calm], "steps_up": []}}}], {}, "F", settings())
    assert figs["mash"]["state"] == "caution"
    assert figs["mash"]["came_over"] == [{"brew": "a", "target": 152.0, "over": 0.9}]
    calm_only = ledger.all_figures([{"id": "b", "cells": {"mash": {"rests": [calm], "steps_up": []}}}], {}, "F",
                                   settings())
    assert calm_only["mash"]["state"] == "nothing"


def volume(eid, stage, value, temp=None, **more):
    data = dict({"measurement_stage": stage, "value": value, "unit": "us_gal"}, **more)
    if temp is not None:
        data.update(temperature=temp, temperature_unit="f")
    return {"entry_id": eid, "kind": "volume", "sequence": 1, "data": data}


def test_a_volume_at_168_f_is_less_at_68_f():
    assert ledger.at_68(10.0, (168.0 - 32) * 5 / 9) == pytest.approx(9.76, abs=0.005)


def test_grain_absorption_strike_from_the_hlts_fill():
    found = ledger.volumes([volume("f", "strike_water", 15.0, 68.0),
                            volume("s", "sparge_water", 5.4, 68.0, level_before=9.9, level_after=4.5),
                            volume("p", "pre_boil", 8.6, 68.0)])
    cell = ledger.grain_cell(found, {}, {"grain_kg": 13.5 / 2.2046226218, "lauter_dead_l": 0.5 * 3.785411784}, "F")
    assert cell["strike_from"] == "hlt_fill" and cell["hlt_fill"] == 15.0 and cell["sparge_before"] == 9.9
    assert cell["strike"] == 5.1 and cell["sparge"] == 5.4 and cell["pre_boil"] == 8.6 and cell["dead"] == 0.5
    assert cell["held"] == 1.4 and cell["per"] == pytest.approx(1.4 / 13.5, abs=0.0005) and cell["grain"] == 13.5


def test_grain_absorption_takes_a_probes_reading_for_a_volume_without_its_temperature():
    found = ledger.volumes([volume("f", "strike_water", 15.0, 68.0),
                            volume("s", "sparge_water", 5.4, None, level_before=9.9),
                            volume("p", "pre_boil", 8.6, 68.0)])
    cell = ledger.grain_cell(found, {"s": 168.0}, {"grain_kg": 6.0}, "F")
    assert cell["sparge_before"] == pytest.approx(ledger.at_68(9.9, (168.0 - 32) * 5 / 9), abs=0.005)


def test_grain_absorption_says_why_not():
    fill, pre = volume("f", "strike_water", 15.0), volume("p", "pre_boil", 8.6)
    no_levels = ledger.volumes([fill, volume("s", "sparge_water", 5.4), pre])
    assert ledger.grain_cell(no_levels, {}, {"grain_kg": 6}, "F") == {"none": "no_levels"}
    assert ledger.grain_cell(ledger.volumes([fill, pre]), {}, {"grain_kg": 6}, "F") == {"none": "no_volumes"}
    levels = ledger.volumes([fill, volume("s", "sparge_water", 5.4, level_before=9.9), pre])
    assert ledger.grain_cell(levels, {}, {}, "F") == {"none": "no_grain_weight"}


def test_a_withdrawn_or_corrected_reading_doesnt_stand():
    first = volume("p1", "pre_boil", 8.6)
    fixed = volume("p2", "pre_boil", 8.9, replaces="p1")
    fixed["data"]["replaces"] = "p1"
    assert ledger.volumes([first, fixed])["pre_boil"]["entry_id"] == "p2"
    gone = {"entry_id": "w", "kind": "withdrawal", "sequence": 2, "data": {"withdraws": ["p2"]}}
    assert "pre_boil" not in ledger.volumes([first, fixed, gone])


def test_the_active_step_is_logged_each_time_it_changes():
    logged = []
    controller = StepController.__new__(StepController)
    controller.cbpi = types.SimpleNamespace(
        log=types.SimpleNamespace(log_change=lambda series, value: logged.append((series, value))),
        ws=types.SimpleNamespace(send=lambda m: None), push_update=lambda **k: None)
    controller.profile = [Step("a", "Heat", status=StepState.INITIAL), Step("b", "Mash", status=StepState.ACTIVE)]
    controller.push_udpate()
    controller.profile[1].status = StepState.DONE
    controller.push_udpate()
    assert logged == [("steps.active", 2), ("steps.active", 0)]
