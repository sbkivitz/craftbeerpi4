"""The recipe's mash figures changed in the recipe editor, and the one copy of them MashIn works from.

The recipe editor (Brew Steps, for the loaded recipe) shows the recipe's grain weight and mash water (the strike water to
the mash tun) beside BrewStart's HLT water, and changes them through the strike water helper's own command, so the
strike water box has them at once:

- POST /step2/strike/recipe with "edit": true may change BeerSmith's own figure, which Brew Day's box may not. The
  figure is then named in typed, as it isn't BeerSmith's, and BeerSmith's own is kept in mash_numbers' beersmith; the
  state's recipe.changed gives it, in the state's units, for each figure changed from it (an empty dict where none is,
  so a screen can tell a CraftBeerPi that takes "edit").
- "back": [...] puts BeerSmith's own back, BeerSmith's again; so does changing the figure to BeerSmith's own. There's
  nothing to go back to for a figure BeerSmith never gave.
- Mash water that would leave the HLT less than the least that covers its element and coil is refused, as on Brew Day;
  BeerSmith's own is never refused.
- MashInStep works its strike out from the recipe's figures where the recipe has them, its own Grain_Kg and Mash_Water
  only where it has none; and a figure given clears a MashIn still to come's own copy, so there's no second copy to
  disagree.
"""

import types

from cbpi.controller.strike_controller import StrikeController
from cbpi.extension.mashstep import MashInStep
from cbpi.strike import model

from tests.test_strike_typed import BEERSMITH, Brewery

GAL = round(BEERSMITH["infuse_l"] / model.L_PER_GAL, 2)
LB = round(BEERSMITH["grain_kg"] / model.LB, 1)


def test_the_state_says_no_figure_is_changed_from_beersmiths(tmp_path, monkeypatch):
    st = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH).state()
    assert st["recipe"]["changed"] == {} and st["recipe"]["typed"] == []


def test_the_editor_changes_beersmiths_mash_water_and_keeps_beersmiths_own(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    b.state()
    out = b.send("set_recipe", strike_vol=8.0, edit=True)
    numbers = b.basic["mash_numbers"]
    assert numbers["infuse_l"] == round(8.0 * model.L_PER_GAL, 3)
    assert numbers["typed"] == ["water"] and numbers["beersmith"] == {"infuse_l": BEERSMITH["infuse_l"]}
    assert numbers["grain_kg"] == BEERSMITH["grain_kg"] and "beersmith" not in BEERSMITH
    b.step.save.assert_awaited()
    st = out["strike"]
    assert (st["recipe"]["strike_vol"], st["recipe"]["changed"], st["recipe"]["typed"]) == (8.0, {"strike_vol": GAL},
                                                                                          ["water"])
    assert out["applied"] == {"what": "recipe", "strike_vol": 8.0, "from": {"strike_vol": GAL}}
    assert b.jobs and abs(b.jobs[-1][0]["strike_gal"] - 8.0) < 1e-3, "USE is worked out from the new mash water at once"
    # changed again: BeerSmith's own stays the one kept
    b.send("set_recipe", strike_vol=7.5, edit=True)
    assert b.basic["mash_numbers"]["beersmith"] == {"infuse_l": BEERSMITH["infuse_l"]}


def test_every_screen_hears_the_recipes_basic_data_with_the_figures(tmp_path, monkeypatch):
    from unittest import mock
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    b.step.push_udpate = mock.Mock()
    b.send("set_recipe", strike_vol=8.0, edit=True)
    b.step.push_udpate.assert_called_with(complete=True)


def test_without_edit_beersmiths_own_is_still_refused(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    status, body = b.refused("set_recipe", strike_vol=8.0)
    assert (status, body["error"], body["what"]) == (409, "beersmith", "strike_vol")
    assert b.basic["mash_numbers"] == BEERSMITH


def test_the_grain_weight_changed_and_both_put_back(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    b.send("set_recipe", grain=24.0, strike_vol=8.0, edit=True)
    st = b.state()
    assert st["recipe"]["changed"] == {"grain": LB, "strike_vol": GAL} and st["recipe"]["grain_from"] == "typed"
    out = b.send("set_recipe", back=["strike_vol", "grain"])
    numbers = b.basic["mash_numbers"]
    assert (numbers["infuse_l"], numbers["grain_kg"]) == (BEERSMITH["infuse_l"], BEERSMITH["grain_kg"])
    assert numbers["typed"] == [] and "beersmith" not in numbers
    assert out["applied"] == {"what": "recipe", "strike_vol": GAL, "grain": LB, "back": ["strike_vol", "grain"],
                              "from": {"strike_vol": 8.0, "grain": 24.0}}
    st = out["strike"]
    assert (st["recipe"]["changed"], st["recipe"]["grain_from"], st["recipe"]["strike_vol"]) == ({}, "beersmith", GAL)


def test_a_figure_changed_to_beersmiths_own_is_beersmiths_again(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    b.send("set_recipe", strike_vol=8.0, edit=True)
    out = b.send("set_recipe", strike_vol=GAL, edit=True)
    numbers = b.basic["mash_numbers"]
    assert numbers["infuse_l"] == BEERSMITH["infuse_l"] and numbers["typed"] == [] and "beersmith" not in numbers
    assert out["applied"]["back"] == ["strike_vol"]


def test_nothing_to_go_back_to_where_beersmith_gave_no_figure(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, step_kg=9.072)
    b.send("set_recipe", strike_vol=9.0)
    status, body = b.refused("set_recipe", back=["strike_vol"])
    assert (status, body["error"], body["what"]) == (409, "no_beersmith", "strike_vol")
    status, body = b.refused("set_recipe", back=["grain"])
    assert (status, body["error"]) == (409, "no_beersmith")
    assert b.basic["mash_numbers"]["typed"] == ["water"]


def test_a_figure_both_given_and_put_back_or_an_unknown_one_is_a_bad_body(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH)
    for body in ({"strike_vol": 8.0, "back": ["strike_vol"]}, {"back": ["room"]}, {"back": "grain"}, {"back": []}):
        status, answer = b.refused("set_recipe", **body)
        assert (status, answer["error"]) == (400, "bad_body"), body
    assert b.basic["mash_numbers"] == BEERSMITH


def test_mash_water_changed_from_beersmiths_is_held_to_the_hlts_least(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, volume=12, hlt={"capacity": 20})
    status, body = b.refused("set_recipe", strike_vol=9.5, edit=True)
    assert (status, body["error"], body["most"], body["least"]) == (400, "too_much", 9.0, 3.0)
    assert b.basic["mash_numbers"] == BEERSMITH


def test_beersmiths_own_mash_water_is_never_refused(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, volume=15, hlt={"capacity": 20})
    b.send("set_recipe", strike_vol=8.0, edit=True)
    b.strike.cbpi.step.profile[0].props["Volume"] = 11.0
    out = b.send("set_recipe", back=["strike_vol"])
    assert b.basic["mash_numbers"]["infuse_l"] == BEERSMITH["infuse_l"]
    assert out["strike"]["warnings"] == ["hlt_low"], "it leaves less than the least, which is said, not refused"


def test_a_figure_given_clears_the_copy_a_mashin_still_to_come_keeps(tmp_path, monkeypatch):
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, step_kg=8.0)
    mi = b.profile[1]
    mi.props["Mash_Water"] = 30.0
    b.send("set_recipe", strike_vol=8.0, edit=True)
    assert (mi.props["Mash_Water"], mi.props["Grain_Kg"]) == ("", 8.0), "only the figure given"
    b.send("set_recipe", grain=24.0, edit=True)
    assert mi.props["Grain_Kg"] == ""


def test_a_mashin_that_has_begun_keeps_its_copy(tmp_path, monkeypatch):
    from cbpi.api.step import StepState
    b = Brewery(tmp_path, monkeypatch, numbers=BEERSMITH, step_kg=8.0)
    b.profile[1].status = StepState.ACTIVE
    b.send("set_recipe", grain=24.0, edit=True)
    assert b.profile[1].props["Grain_Kg"] == 8.0


def mash_in(props, numbers=None):
    step = object.__new__(MashInStep)
    basic = {} if numbers is None else {"mash_numbers": dict(numbers)}
    step.cbpi = types.SimpleNamespace(step=types.SimpleNamespace(basic_data=basic))
    step.props = dict(props)
    step.name = "MashIn"
    return step


def test_mashin_works_from_the_recipes_figures_where_it_has_them():
    own = {"Grain_Kg": 5.0, "Mash_Water": 15.0}
    assert mash_in(own, BEERSMITH)._grain_and_water() == (BEERSMITH["grain_kg"], BEERSMITH["infuse_l"])
    assert mash_in(own, {"grain_kg": 7.0, "lacks": ["water"]})._grain_and_water() == (7.0, 15.0)
    assert mash_in(own)._grain_and_water() == (5.0, 15.0)
    assert mash_in({}, {"grain_kg": None, "infuse_l": 0})._grain_and_water() == (0.0, 0.0)


def test_mashins_strike_follows_the_recipes_figure_as_it_changes():
    step = mash_in({"Grain_Sensor": "room", "Grain_Kg": 1.0, "Mash_Water": 50.0}, BEERSMITH)
    notes = []
    step.cbpi.notify = lambda *a, **k: notes.append(a)
    step.get_sensor_value = lambda _id: {"value": 68.0}
    step.get_config_value = lambda key, default: "F" if key == "TEMP_UNIT" else default
    first = step._strike_target(152.0)
    step.cbpi.step.basic_data["mash_numbers"]["infuse_l"] = 25.0
    second = step._strike_target(152.0)
    assert first > 152.0 and second > first, "less mash water for the same grain needs hotter water"
    assert str(BEERSMITH["grain_kg"]) in notes[-1][1], "the recipe's grain weight, not the step's own 1 kg"


def test_the_kept_figures_read_only_numbers_more_than_0():
    assert StrikeController._beersmith_kept({"beersmith": {"infuse_l": "x", "grain_kg": 0}}) == {}
    assert StrikeController._beersmith_kept({"beersmith": [1]}) == {}
    assert StrikeController._beersmith_kept({"beersmith": {"infuse_l": 30.0}}) == {"infuse_l": 30.0}


# ------------------------------------------------------------------ a suggestion where the recipe has none

def beerxml(name="Test Recipe v5", grain_kg=9.07184, amount="33.04 qt", ratio="1.500 qt/lb"):
    """A BeerSmith file kept for a recipe: its grain bill and its first infusion's water and ratio."""
    shown = "<DISPLAY_INFUSE_AMT>%s</DISPLAY_INFUSE_AMT>" % amount if amount else ""
    return ('<?xml version="1.0" encoding="UTF-8"?><RECIPES><RECIPE><NAME>%s</NAME><VERSION>1</VERSION>'
            "<FERMENTABLES><FERMENTABLE><NAME>Pale</NAME><TYPE>Grain</TYPE><AMOUNT>%s</AMOUNT></FERMENTABLE>"
            "</FERMENTABLES><MASH><NAME>Single</NAME><MASH_STEPS><MASH_STEP><NAME>Protein Rest</NAME>"
            "<TYPE>Infusion</TYPE>%s<WATER_GRAIN_RATIO>%s</WATER_GRAIN_RATIO></MASH_STEP></MASH_STEPS></MASH>"
            "</RECIPE></RECIPES>" % (name, grain_kg, shown, ratio)).encode("utf-8")


def keep(tmp_path, file_id, basic, xml=None, steps=None):
    import yaml
    folder = tmp_path / "recipes"
    folder.mkdir(exist_ok=True)
    if xml is not None:
        (folder / ("%s.xml" % file_id)).write_bytes(xml)
        basic = dict(basic, source_xml="recipes/%s.xml" % file_id)
    (folder / ("%s.yaml" % file_id)).write_text(yaml.dump({"basic": basic, "steps": steps or []}))
    return basic


def loaded(tmp_path, monkeypatch, basic, **kw):
    b = Brewery(tmp_path, monkeypatch, **kw)
    b.basic.clear()
    b.basic.update(basic)
    b.strike.cbpi.config_folder.get_recipe_file_by_id = lambda file_id: str(tmp_path / "recipes" / (file_id + ".yaml"))
    return b


def suggested(b, file=None):
    import asyncio
    return asyncio.run(b.strike.suggest(file))


def test_a_recipe_with_its_beersmith_file_but_no_figures_is_offered_the_files(tmp_path, monkeypatch):
    basic = keep(tmp_path, "v5", {"name": "Test Recipe v5"}, xml=beerxml())
    b = loaded(tmp_path, monkeypatch, basic)
    out = suggested(b)
    assert (out["mass_unit"], out["vol_unit"]) == ("lb", "gal")
    assert out["strike_vol"] == {"value": 8.26, "from": "file", "says": "from the recipe's BeerSmith file"}
    assert out["grain"] == {"value": 20.0, "from": "file", "says": "from the recipe's BeerSmith file"}
    assert "mash_numbers" not in b.basic, "a suggestion saves nothing"


def test_the_files_figure_taken_is_kept_as_beersmiths(tmp_path, monkeypatch):
    basic = keep(tmp_path, "v5", {"name": "Test Recipe v5"}, xml=beerxml())
    b = loaded(tmp_path, monkeypatch, basic)
    b.send("set_recipe", strike_vol=8.26)
    numbers = b.basic["mash_numbers"]
    assert numbers["infuse_l"] == round(33.04 * 0.946352946, 6) and numbers["typed"] == []
    assert "water" not in numbers["lacks"]
    b.send("set_recipe", grain=21.0)
    assert b.basic["mash_numbers"]["typed"] == ["grain"], "a figure other than the file's is the brewer's"


def test_no_suggestion_for_a_figure_the_recipe_has(tmp_path, monkeypatch):
    basic = keep(tmp_path, "v5", {"name": "Test Recipe v5", "mash_numbers": dict(BEERSMITH)}, xml=beerxml())
    assert suggested(loaded(tmp_path, monkeypatch, basic)) == {"mass_unit": "lb", "vol_unit": "gal", "grain": None,
                                                               "strike_vol": None}


def test_another_book_recipe_of_the_name_with_the_same_grain_bill(tmp_path, monkeypatch):
    import os
    later = {"grain_kg": 9.0, "infuse_l": 31.27, "lacks": []}
    keep(tmp_path, "later", {"name": "test recipe V5", "mash_numbers": later})
    keep(tmp_path, "typed", {"name": "Test Recipe v5", "mash_numbers": {"grain_kg": 9.0, "infuse_l": 40.0,
                                                                       "typed": ["water"]}})
    os.utime(tmp_path / "recipes" / "typed.yaml", (2e9, 2e9))
    when = 1.79e9
    os.utime(tmp_path / "recipes" / "later.yaml", (when, when))
    basic = keep(tmp_path, "mine", {"name": "Test Recipe v5", "book_file": "mine"})
    b = loaded(tmp_path, monkeypatch, basic, step_kg=9.072)
    out = suggested(b)
    assert out["strike_vol"]["value"] == 8.26 and out["strike_vol"]["from"] == "book"
    assert (out["strike_vol"]["file"], out["strike_vol"]["imported"]) == ("later", round(when))
    from cbpi.strike.suggest import day_words
    assert out["strike_vol"]["says"] == "from test recipe V5 imported %s" % day_words(when)
    assert out["grain"]["value"] == round(9.0 / model.LB, 1), "the grain bill the step keeps is matched"


def test_a_book_recipe_whose_grain_bill_differs_or_is_unknown_isnt_offered(tmp_path, monkeypatch):
    keep(tmp_path, "other", {"name": "Test Recipe v5", "mash_numbers": {"grain_kg": 9.5, "infuse_l": 31.27}})
    basic = keep(tmp_path, "mine", {"name": "Test Recipe v5"})
    out = suggested(loaded(tmp_path, monkeypatch, basic, step_kg=9.072))
    assert (out["grain"], out["strike_vol"]) == (None, None), "9.5 kg is more than 2% off 9.072"
    out = suggested(loaded(tmp_path, monkeypatch, basic))
    assert (out["grain"], out["strike_vol"]) == (None, None), "no grain bill of its own to match"


def test_beersmiths_mash_ratio_times_the_grain_weight(tmp_path, monkeypatch):
    basic = keep(tmp_path, "v5", {"name": "Test Recipe v5", "mash_numbers": {"grain_kg": 9.07184, "lacks": ["water"]}},
                 xml=beerxml(amount=None))
    out = suggested(loaded(tmp_path, monkeypatch, basic))
    assert out["grain"] is None
    assert out["strike_vol"] == {"value": 7.5, "from": "ratio", "ratio": 1.5, "ratio_unit": "qt/lb", "grain": 20.0,
                                 "says": "1.5 qt/lb × 20 lb, BeerSmith's mash ratio"}


def test_nothing_is_made_up_without_a_source(tmp_path, monkeypatch):
    out = suggested(loaded(tmp_path, monkeypatch, {"name": "Built here"}, step_kg=9.072))
    assert (out["grain"], out["strike_vol"]) == (None, None)


def test_a_book_recipes_own_suggestion(tmp_path, monkeypatch):
    keep(tmp_path, "v5", {"name": "Test Recipe v5"}, xml=beerxml(),
         steps=[{"type": "BrewStart", "props": {"Volume_Unit": "L"}}])
    b = loaded(tmp_path, monkeypatch, {"name": "Something else loaded"})
    out = suggested(b, "v5")
    assert out["vol_unit"] == "L" and out["strike_vol"]["value"] == round(33.04 * 0.946352946, 2)
    from cbpi.controller.strike_controller import StrikeRefused
    for file_id in ("nope", "../v5", ""):
        try:
            suggested(b, file_id)
            raise AssertionError("not refused: %r" % file_id)
        except StrikeRefused as refused:
            assert (refused.status, refused.body()["error"]) == (404, "not_found")
