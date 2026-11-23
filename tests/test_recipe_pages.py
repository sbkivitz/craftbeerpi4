"""The recipe pages' server changes (4.7.7.post59).

- An import is kept in the Recipe Book and leaves the brew steps alone: it answers {name, file, replaced, warnings},
  and a refusal says nothing was added to the Recipe Book.
- The brew steps know which book recipe they are a copy of: LOAD records book_file, loaded_at and book_modified,
  book_data() leaves them out, and SAVE TO BOOK replaces that recipe or saves the steps as a new one.
- A BeerXML import keeps the figures for the HLT's water in mash_numbers, and GRAIN_ABSORPTION is a setting in the
  brewery's units.
- The book keeps the recipe, not this brew: book_data() puts a BrewStart's day given as a date in as Tomorrow, the
  strike water as it was before the first USE (StrikeController.recipe_temps), and BrewStart's grain temperature
  blank, while the brew steps keep all three.
- Where the grain spent the night is this brew's answer (grain_place): POST /step2/strike/grain keeps it, a load
  clears it, and the book never gets it.

Each test fails on its own on a core before post59.
"""

import asyncio
import json
import os
import types
import xml.etree.ElementTree as ET
from unittest import mock

import pytest
import yaml

import cbpi.controller.step_controller as step_controller
from cbpi.api import clock
from cbpi.api.dataclasses import NotificationType, Props, Step, StepState
from cbpi.controller.recipe_controller import RecipeController
from cbpi.controller.step_controller import StepController
from cbpi.controller.upload_controller import UploadController
from cbpi.extension.ConfigUpdate import ConfigUpdate
from cbpi.http_endpoints.http_step import StepHttpEndpoints
from cbpi.http_endpoints.http_upload import UploadHttpEndpoints

BookRefused = getattr(step_controller, "BookRefused", None)
QT = 0.946352946
BOOK_KEYS = {"book_file", "loaded_at", "book_modified"}
WARNING = ("Not every boil addition has a reminder", "This recipe has 7 boil additions, but the boil step can remind "
           "you of only six.")


class Folder:
    """config_folder on a temporary folder: the Recipe Book in recipes/."""

    def __init__(self, root):
        self.root = root
        (root / "recipes").mkdir(exist_ok=True)

    def get_file_path(self, name):
        return str(self.root / name)

    def get_recipe_file_by_id(self, file):
        return str(self.root / "recipes" / "{}.yaml".format(file))


def brew_steps(tmp_path, basic=None):
    """The brew steps, one step waiting, on a server stand-in with a Recipe Book."""
    steps = StepController.__new__(StepController)
    steps.profile = [Step("a", "MashIn", type="MashInStep", status=StepState.INITIAL, props=Props({"Temp": 126}))]
    steps.basic_data = dict(basic if basic is not None else {"name": "Craftbeerpi Test Recipe v5"})
    steps.path = str(tmp_path / "step_data.json")
    steps.types = {}
    steps.cbpi = types.SimpleNamespace(config_folder=Folder(tmp_path), notify=mock.Mock(), ws=mock.Mock())
    steps.cbpi.step = steps
    steps.shutdown = mock.AsyncMock()
    steps.load = mock.Mock()
    steps._retire_prompts = mock.Mock()
    steps.push_udpate = mock.Mock()
    steps._tell = mock.Mock()
    steps.save = mock.AsyncMock()
    return steps


def in_book(tmp_path, file, data):
    path = tmp_path / "recipes" / "{}.yaml".format(file)
    path.write_text(yaml.dump(data))
    return path


def book_files(tmp_path):
    return sorted(p.name for p in (tmp_path / "recipes").iterdir())


# ---------------------------------------------------------------------------------------------------- the import

def uploader(read_steps=None, held=(WARNING,), kept=("book-1", True)):
    """An UploadController whose BeerXML reader gives `read_steps` and the warnings `held`, its Recipe Book keeping
    the recipe as `kept` says (file, replaced)."""
    upload = UploadController.__new__(UploadController)
    cbpi = mock.Mock()
    cbpi.config.get.side_effect = lambda key, default=None: default
    cbpi.recipe.keep_import = mock.AsyncMock(return_value=kept)
    cbpi.step.load_recipe = mock.AsyncMock()
    cbpi.step.running_step.return_value = None
    upload.cbpi = cbpi
    upload._import_lock = asyncio.Lock()
    upload._staging, upload._staged, upload._import_error, upload._import_warnings = False, None, None, []
    upload.get_config_values = mock.Mock()
    upload._import_problem = mock.Mock(return_value=None)
    upload._with_mash_out = lambda found: found
    steps = read_steps if read_steps is not None else [
        {"name": "Protein Rest", "type": "MashStep", "props": {"Temp": 126}, "status": "I", "status_text": ""}]

    async def read(recipe_id):
        upload._staged = {"name": "Craftbeerpi Test Recipe v6", "steps": list(steps),
                          "mash_numbers": {"grain_kg": 9.07184, "boil_l": 87.064973, "lacks": []}}
        for title, text in held:
            upload._import_warning(title, text)

    upload.xml_recipe_creation = read
    return upload, cbpi


def test_an_import_is_kept_in_the_book_and_answers_its_file_whether_it_replaced_and_its_warnings():
    upload, cbpi = uploader()
    ok, answer = asyncio.run(upload.import_recipe("xml", "1"))
    assert ok is True
    assert answer == {"name": "Craftbeerpi Test Recipe v6", "file": "book-1", "replaced": True,
                      "warnings": [{"title": WARNING[0], "text": WARNING[1]}]}
    cbpi.step.load_recipe.assert_not_awaited()
    kept, source = cbpi.recipe.keep_import.await_args.args
    assert source == "BeerXML"
    assert kept["basic"]["mash_numbers"]["boil_l"] == 87.064973
    assert kept["steps"] == [{"name": "Protein Rest", "type": "MashStep", "status": "I", "props": {"Temp": 126}}]
    said = [c.args[:2] for c in cbpi.notify.call_args_list]
    assert said == [("BeerXML recipe imported",
                     "Craftbeerpi Test Recipe v6. Its earlier copy in your Recipe Book was replaced."), WARNING]


def test_a_refused_import_says_nothing_was_added_to_the_book():
    upload, cbpi = uploader(read_steps=[])
    ok, why = asyncio.run(upload.import_recipe("xml", "1"))
    assert ok is False
    assert why == "The BeerXML recipe was not imported: it gave no steps. Nothing was added to the Recipe Book."
    cbpi.recipe.keep_import.assert_not_awaited()
    cbpi.notify.assert_called_once_with("Recipe not imported", why, NotificationType.ERROR)


def test_a_book_that_cant_be_written_means_the_recipe_was_not_imported():
    upload, cbpi = uploader()
    cbpi.recipe.keep_import.side_effect = OSError("disk full")
    ok, why = asyncio.run(upload.import_recipe("xml", "1"))
    assert ok is False
    assert why == ("The BeerXML recipe could not be kept in the Recipe Book: disk full. Nothing was added to the "
                   "Recipe Book.")
    cbpi.step.load_recipe.assert_not_awaited()
    assert [c.args[0] for c in cbpi.notify.call_args_list] == ["Recipe not imported"]


def test_the_import_endpoints_answer_the_result_or_why_nothing_was_added():
    answer = {"name": "v6", "file": "book-1", "replaced": False, "warnings": []}
    res = UploadHttpEndpoints._import_result(True, answer)
    assert res.status == 200 and json.loads(res.text) == answer
    res = UploadHttpEndpoints._import_result(False, "Why. Nothing was added to the Recipe Book.")
    assert res.status == 400
    assert json.loads(res.text) == {"error": "recipe not imported",
                                    "detail": "Why. Nothing was added to the Recipe Book."}


# ------------------------------------------------------------------------------------- the steps' book recipe

def test_load_records_the_book_file_when_it_was_loaded_and_the_files_modified_time(tmp_path):
    steps = brew_steps(tmp_path)
    path = in_book(tmp_path, "v5", {"basic": {"name": "v5", "book_file": "stale", "loaded_at": 1, "book_modified": 2},
                                    "steps": [{"name": "Rest", "type": "MashStep", "props": {}}]})
    os.utime(path, (1_700_000_000, 1_700_000_000))
    with mock.patch.object(clock, "now", return_value=1_800_000_123.5):
        asyncio.run(RecipeController(steps.cbpi).brew("v5"))
    basic = json.loads((tmp_path / "step_data.json").read_text())["basic"]
    assert basic["book_file"] == "v5"
    assert basic["loaded_at"] == 1_800_000_123.5
    assert basic["book_modified"] == os.path.getmtime(path) == 1_700_000_000


def test_steps_loaded_other_than_from_the_book_know_no_book_recipe(tmp_path):
    steps = brew_steps(tmp_path)
    asyncio.run(steps.load_recipe({"basic": {"name": "x", "book_file": "stale", "loaded_at": 1, "book_modified": 2},
                                   "steps": []}))
    basic = json.loads((tmp_path / "step_data.json").read_text())["basic"]
    assert basic == {"name": "x"}


def test_the_book_never_keeps_which_book_recipe_or_when(tmp_path):
    steps = brew_steps(tmp_path, {"name": "v5", "book_file": "v5", "loaded_at": 1.0, "book_modified": 2.0,
                                  "mash_numbers": {"grain_kg": 9.07}})
    assert steps.book_data()["basic"] == {"name": "v5", "mash_numbers": {"grain_kg": 9.07}}
    assert steps.basic_data["book_file"] == "v5"


def test_save_to_book_in_place_of_the_book_recipe(tmp_path):
    steps = brew_steps(tmp_path, {"name": "v5", "book_file": "v5", "loaded_at": 1.0, "book_modified": 2.0})
    in_book(tmp_path, "v5", {"basic": {"name": "older"}, "steps": []})
    assert asyncio.run(steps.savetobook(file="v5")) == "v5"
    kept = yaml.safe_load((tmp_path / "recipes" / "v5.yaml").read_text())
    assert kept["basic"] == {"name": "v5"}
    assert [s["name"] for s in kept["steps"]] == ["MashIn"]
    assert book_files(tmp_path) == ["v5.yaml"]
    assert steps.basic_data == {"name": "v5", "book_file": "v5", "loaded_at": 1.0,
                                "book_modified": os.path.getmtime(tmp_path / "recipes" / "v5.yaml")}
    steps.save.assert_awaited_once()
    assert pushed_basic(steps) == [steps.basic_data]


def pushed_basic(steps):
    """The basic data of every mash_profile_update sent to the pages - the only topic that carries it."""
    return [call.args[0]["data"]["basic"] for call in steps.cbpi.ws.send.call_args_list
            if call.args[0].get("topic") == "mash_profile_update"]


def test_save_to_book_as_a_new_recipe_names_the_steps_too(tmp_path):
    steps = brew_steps(tmp_path, {"name": "v5", "book_file": "v5"})
    book_id = asyncio.run(steps.savetobook(name="  Iodine test "))
    assert book_files(tmp_path) == ["{}.yaml".format(book_id)]
    kept = yaml.safe_load((tmp_path / "recipes" / "{}.yaml".format(book_id)).read_text())
    assert kept["basic"] == {"name": "Iodine test"}
    assert steps.basic_data["name"] == "Iodine test" and steps.basic_data["book_file"] == book_id
    steps.save.assert_awaited_once()
    assert [b.get("name") for b in pushed_basic(steps)] == ["Iodine test"]
    assert pushed_basic(steps)[0]["book_file"] == book_id


def test_save_to_book_with_neither_saves_a_new_copy_as_before_and_answers_its_file(tmp_path):
    steps = brew_steps(tmp_path, {"name": "v5"})
    book_id = asyncio.run(steps.savetobook())
    assert book_files(tmp_path) == ["{}.yaml".format(book_id)]
    assert yaml.safe_load((tmp_path / "recipes" / "{}.yaml".format(book_id)).read_text())["basic"] == {"name": "v5"}
    assert steps.basic_data == {"name": "v5"}
    steps.save.assert_not_awaited()
    steps.push_udpate.assert_called_once()
    assert pushed_basic(steps) == []


@pytest.mark.parametrize("kwargs, status, error", [
    (dict(file="v5", name="New"), 400, "bad_body"),
    (dict(file="../v5"), 400, "bad_body"),
    (dict(file=""), 400, "bad_body"),
    (dict(file=5), 400, "bad_body"),
    (dict(file="gone"), 404, "not_in_book"),
    (dict(name="   "), 400, "bad_body"),
    (dict(name=7), 400, "bad_body"),
])
def test_save_to_book_refused_changes_nothing(tmp_path, kwargs, status, error):
    if BookRefused is None:
        pytest.fail("no BookRefused: SAVE TO BOOK takes neither file nor name before 4.7.7.post59")
    steps = brew_steps(tmp_path, {"name": "v5", "book_file": "v5"})
    path = in_book(tmp_path, "v5", {"basic": {"name": "older"}, "steps": []})
    before = path.read_bytes()
    with pytest.raises(BookRefused) as refused:
        asyncio.run(steps.savetobook(**kwargs))
    assert (refused.value.status, refused.value.error) == (status, error)
    assert steps.basic_data == {"name": "v5", "book_file": "v5"}
    assert book_files(tmp_path) == ["v5.yaml"] and path.read_bytes() == before
    steps.save.assert_not_awaited()
    assert pushed_basic(steps) == []


def test_save_to_book_refusals_say_why_in_the_pages_words(tmp_path):
    if BookRefused is None:
        pytest.fail("no BookRefused: SAVE TO BOOK takes neither file nor name before 4.7.7.post59")
    steps = brew_steps(tmp_path)
    with pytest.raises(BookRefused) as gone:
        asyncio.run(steps.savetobook(file="gone"))
    assert gone.value.body() == {"error": "not_in_book", "file": "gone", "detail": (
        "That recipe is no longer in the Recipe Book: it may have been deleted on another screen. Nothing was saved.")}
    with pytest.raises(BookRefused) as blank:
        asyncio.run(steps.savetobook(name=""))
    assert blank.value.body() == {"error": "bad_body",
                                  "detail": "Give it a name first: the Recipe Book lists recipes by name."}


class Request:
    """A POST to /step2/savetobook: `raw`, its body, or None for none."""

    def __init__(self, raw):
        self.raw = raw
        self.body_exists = raw is not None

    async def read(self):
        return self.raw or b""


def test_the_save_to_book_endpoint_answers_the_file_and_refuses_a_body_it_cant_use(tmp_path):
    steps = brew_steps(tmp_path, {"name": "v5"})
    in_book(tmp_path, "v5", {"basic": {"name": "v5"}, "steps": []})
    ep = StepHttpEndpoints.__new__(StepHttpEndpoints)
    ep.controller = steps
    res = asyncio.run(ep.http_savetobook(Request(b'{"file": "v5"}')))
    assert res.status == 200 and json.loads(res.text) == {"file": "v5"}
    for raw in (b"not json", b'["v5"]', b'{"file": "v5", "fiel": 1}'):
        res = asyncio.run(ep.http_savetobook(Request(raw)))
        assert res.status == 400 and json.loads(res.text)["error"] == "bad_body", raw
    res = asyncio.run(ep.http_savetobook(Request(b'{"file": "gone"}')))
    assert res.status == 404 and json.loads(res.text)["error"] == "not_in_book"
    for raw in (None, b"", b"{}"):
        res = asyncio.run(ep.http_savetobook(Request(raw)))
        assert res.status == 200 and set(json.loads(res.text)) == {"file"}, raw


# -------------------------------------------------------------------------------- the figures for the HLT's water

BEER = """<RECIPE><NAME>Craftbeerpi Test Recipe v5</NAME><BOIL_SIZE>87.0649728</BOIL_SIZE>
<FERMENTABLES><FERMENTABLE><TYPE>Grain</TYPE><AMOUNT>9.0718400</AMOUNT></FERMENTABLE></FERMENTABLES>
<EQUIPMENT><BOIL_SIZE>87.0649728</BOIL_SIZE><TOP_UP_KETTLE>0.0000000</TOP_UP_KETTLE>
<LAUTER_DEADSPACE>0.0000000</LAUTER_DEADSPACE></EQUIPMENT>
<MASH><MASH_STEPS>
<MASH_STEP><NAME>Protein Rest</NAME><TYPE>Infusion</TYPE><INFUSE_AMOUNT>28.3907520</INFUSE_AMOUNT>
<DISPLAY_INFUSE_AMT>33.04 qt</DISPLAY_INFUSE_AMT></MASH_STEP>
<MASH_STEP><NAME>Saccharification</NAME><TYPE>Temperature</TYPE><INFUSE_AMOUNT>0.0000000</INFUSE_AMOUNT>
<DISPLAY_INFUSE_AMT>0.00 qt</DISPLAY_INFUSE_AMT></MASH_STEP>
<MASH_STEP><NAME>Sparge</NAME><TYPE>Temperature</TYPE><INFUSE_AMOUNT>9.4635840</INFUSE_AMOUNT>
<DISPLAY_INFUSE_AMT>10.00 qt</DISPLAY_INFUSE_AMT></MASH_STEP>
</MASH_STEPS></MASH></RECIPE>"""


def numbers_of(xml_text):
    return UploadController(types.SimpleNamespace()).getMashNumbers(ET.fromstring(xml_text))


def test_a_beerxml_import_keeps_the_figures_for_the_hlts_water():
    numbers = numbers_of(BEER)
    assert numbers["boil_l"] == pytest.approx(87.0649728, abs=1e-6)
    assert numbers["top_up_kettle_l"] == 0 and numbers["lauter_dead_l"] == 0
    assert numbers["infusions_l"] == pytest.approx((33.04 + 10.00) * QT, abs=1e-5)
    assert numbers["infuse_l"] == pytest.approx(33.04 * QT, abs=1e-5)
    assert numbers["lacks"] == ["tun", "infuse_temp", "grain_temp", "tun_temp"]


def test_a_figure_the_file_hasnt_got_is_left_out():
    start, end = BEER.index("<EQUIPMENT>"), BEER.index("</EQUIPMENT>") + len("</EQUIPMENT>")
    no_equipment = BEER[:start] + BEER[end:]
    numbers = numbers_of(no_equipment)
    assert numbers["boil_l"] == pytest.approx(87.0649728, abs=1e-6)
    assert not {"top_up_kettle_l", "lauter_dead_l"} & set(numbers)
    start, end = no_equipment.index("<MASH>"), no_equipment.index("</MASH>") + len("</MASH>")
    bare = (no_equipment[:start] + no_equipment[end:]).replace("<BOIL_SIZE>87.0649728</BOIL_SIZE>", "")
    assert not {"boil_l", "infusions_l", "infuse_l"} & set(numbers_of(bare))


class Config:
    """The settings, recording what ConfigUpdate adds: `cache` holds an entry for each, with its value."""

    def __init__(self, values):
        self.values, self.added = dict(values), {}
        self.cache = {name: types.SimpleNamespace(value=value) for name, value in self.values.items()}

    def get(self, name, default=None):
        return self.values.get(name, default)

    async def add(self, name, value, type=None, description="", source=None, options=None):
        self.values[name] = value
        self.cache[name] = types.SimpleNamespace(value=value)
        self.added[name] = dict(value=value, description=description, source=source)


def configured(tmp_path, values):
    cbpi = types.SimpleNamespace(
        config=Config(values), static_config={"mqtt_offset": False},
        plugin=types.SimpleNamespace(load_plugin_list=mock.AsyncMock(return_value=[])),
        config_folder=types.SimpleNamespace(get_dashboard_path=lambda name: str(tmp_path / name),
                                            get_file_path=lambda name: str(tmp_path / name)))
    update = ConfigUpdate.__new__(ConfigUpdate)
    update.cbpi = cbpi
    asyncio.run(update.run())
    return cbpi.config


@pytest.mark.parametrize("unit, value, words", [("F", 0.12, "gallons per pound"), ("C", 1.0, "litres per kilogram")])
def test_grain_absorption_is_a_setting_in_the_brewerys_units(tmp_path, unit, value, words):
    added = configured(tmp_path, {"TEMP_UNIT": unit}).added.get("GRAIN_ABSORPTION")
    assert added is not None and added["value"] == value and added["source"] == "steps"
    assert words in added["description"]


def test_a_saved_grain_absorption_is_kept_and_its_description_follows_the_code(tmp_path):
    added = configured(tmp_path, {"TEMP_UNIT": "F", "GRAIN_ABSORPTION": 0.15,
                                  "CONFIG_STATUS": "4.7.7.post1"}).added.get("GRAIN_ABSORPTION")
    assert added is not None and added["value"] == 0.15 and "gallons per pound" in added["description"]


# -- The book keeps the recipe, not this brew ----------------------------------------------------------------------

def tonights_steps(tmp_path, strike=None, mode="Date"):
    """Brew steps as Brew Day leaves them: BrewStart done, with its day as a date and the grain's temperature, and
    the strike water step and BrewStart holding USE's 132.4 F."""
    steps = brew_steps(tmp_path)
    steps.profile = [
        Step("bs", "BrewStart", type="BrewStart", status=StepState.DONE, props=Props({
            "Timing": "Ready by", "Mode": mode, "StartDate": "2026-11-23", "StartTime": "07:00",
            "Ready_Temp": 132.4, "Grain_Temp": 64.0, "Volume": 15})),
        Step("sw", "Strike water", type="MashInStep", status=StepState.INITIAL, props=Props({"Temp": 132.4})),
        Step("mi", "MashIn", type="MashInStep", status=StepState.INITIAL,
             props=Props({"Temp": 126, "Mode": "Date", "Grain_Temp": 64.0}))]
    if strike is not None:
        steps.cbpi.strike = strike
    return steps


def booked(steps):
    return {s["id"]: s["props"] for s in steps.book_data()["steps"]}


def test_the_book_gets_brewstarts_date_as_tomorrow_at_its_time_and_no_grain_temperature(tmp_path):
    steps = tonights_steps(tmp_path)
    book = booked(steps)
    assert (book["bs"]["Mode"], book["bs"]["StartDate"], book["bs"]["StartTime"]) == ("Tomorrow", "", "07:00")
    assert book["bs"]["Grain_Temp"] == ""
    assert (book["bs"]["Timing"], book["bs"]["Volume"]) == ("Ready by", 15)
    assert book["mi"] == {"Temp": 126, "Mode": "Date", "Grain_Temp": 64.0}
    live = steps.profile[0].props
    assert (live["Mode"], live["StartDate"], live["Grain_Temp"]) == ("Date", "2026-11-23", 64.0)


@pytest.mark.parametrize("mode, kept", [("DateTime", "Tomorrow"), ("Today", "Today"), ("Tomorrow", "Tomorrow")])
def test_only_a_day_given_as_a_date_goes_in_as_tomorrow(tmp_path, mode, kept):
    book = booked(tonights_steps(tmp_path, mode=mode))
    assert book["bs"]["Mode"] == kept
    assert book["bs"]["StartDate"] == ("" if kept != mode else "2026-11-23")


def test_the_book_gets_the_strike_water_as_the_strike_helper_had_it_before_use(tmp_path):
    before_use = {"sw": {"Temp": 126.0}, "bs": {"Ready_Temp": 126.0}}
    strike = types.SimpleNamespace(recipe_temps=mock.Mock(return_value=before_use))
    steps = tonights_steps(tmp_path, strike)
    book = booked(steps)
    assert (book["sw"]["Temp"], book["bs"]["Ready_Temp"], book["mi"]["Temp"]) == (126.0, 126.0, 126)
    strike.recipe_temps.assert_called_once_with(steps.profile)
    assert (steps.profile[1].props["Temp"], steps.profile[0].props["Ready_Temp"]) == (132.4, 132.4)


def test_a_strike_helper_that_cant_say_leaves_the_strike_water_as_it_is(tmp_path):
    strike = types.SimpleNamespace(recipe_temps=mock.Mock(side_effect=RuntimeError("no brew day")))
    book = booked(tonights_steps(tmp_path, strike))
    assert (book["sw"]["Temp"], book["bs"]["Ready_Temp"]) == (132.4, 132.4)


def strike_helper(tmp_path, unit="F"):
    from cbpi.controller.strike_controller import StrikeController
    cbpi = types.SimpleNamespace(
        config=types.SimpleNamespace(get=lambda key, default=None: {"TEMP_UNIT": unit}.get(key, default)),
        config_folder=Folder(tmp_path), step=types.SimpleNamespace(save=mock.AsyncMock()))
    return StrikeController(cbpi, watch=False)


def flow(ready=126, strike="126"):
    """BrewStart (Ready by) and the strike water step, as rig page 9b sets them up, before MashIn."""
    return [Step("bs", "BrewStart", type="BrewStart", status=StepState.INITIAL,
                 props=Props({"Timing": "Ready by", "Ready_Temp": ready})),
            Step("sw", "Strike water", type="MashInStep", status=StepState.INITIAL, props=Props({"Temp": strike})),
            Step("mi", "MashIn", type="MashInStep", status=StepState.INITIAL, props=Props({"Temp": 126}))]


def use(helper, profile, do, to, planned, unit="F"):
    helper.day["steps"] = [s.id for s in profile]
    look = types.SimpleNamespace(unit=unit, si=profile[1], bs=profile[0], hlt=None, by="step",
                                 inputs={"planned": planned})
    return asyncio.run(helper._apply_strike(look, do, to))


def test_use_keeps_where_it_wrote_so_the_book_gets_the_strike_water_as_before_the_first_use(tmp_path):
    helper, profile = strike_helper(tmp_path), flow()
    use(helper, profile, "use", 132.4, 126.0)
    before = {"sw": {"Temp": 126.0}, "bs": {"Ready_Temp": 126.0}}
    assert helper.recipe_temps(profile) == before
    # A second USE once BrewStart is done writes the step alone; BrewStart still holds the first USE's figure.
    profile[0].status = StepState.DONE
    use(helper, profile, "use", 131.8, 132.4)
    assert (profile[1].props["Temp"], profile[0].props["Ready_Temp"]) == (131.8, 132.4)
    assert helper.recipe_temps(profile) == before
    restarted = strike_helper(tmp_path)
    restarted._load_day()
    assert restarted.recipe_temps(profile) == before


def test_a_strike_temperature_typed_over_uses_goes_into_the_book_as_typed(tmp_path):
    helper, profile = strike_helper(tmp_path), flow()
    use(helper, profile, "use", 132.4, 126.0)
    profile[1].props["Temp"] = 140
    assert helper.recipe_temps(profile) == {"bs": {"Ready_Temp": 126.0}}


def test_after_back_to_or_on_other_steps_the_book_gets_the_steps_as_they_are(tmp_path):
    helper, profile = strike_helper(tmp_path), flow()
    use(helper, profile, "use", 132.4, 126.0)
    assert helper.recipe_temps(list(reversed(profile))) == {}
    use(helper, profile, "back", 126.0, 132.4)
    assert helper.recipe_temps(profile) == {} and helper.day["used"] is None


def test_the_strike_water_before_use_on_a_brewery_in_c(tmp_path):
    helper, profile = strike_helper(tmp_path, "C"), flow(ready=52.2, strike=52.2)
    use(helper, profile, "use", 55.8, 52.2, unit="C")
    assert helper.recipe_temps(profile) == {"sw": {"Temp": 52.2}, "bs": {"Ready_Temp": 52.2}}


# -- Where the grain spent the night: Brew Day asks every brew -----------------------------------------------------

def test_a_load_starts_a_brew_with_nothing_answered_about_the_grain(tmp_path):
    steps = brew_steps(tmp_path)
    asyncio.run(steps.load_recipe({"basic": {"name": "x", "grain_place": "indoors"}, "steps": []}))
    assert json.loads((tmp_path / "step_data.json").read_text())["basic"] == {"name": "x"}


def test_the_book_never_keeps_where_tonights_grain_spent_the_night(tmp_path):
    steps = brew_steps(tmp_path, {"name": "v5", "grain_place": "garage"})
    assert steps.book_data()["basic"] == {"name": "v5"}
    assert steps.basic_data["grain_place"] == "garage"


def grain_answer(tmp_path, basic=None, grain_temp="", brewstart=True):
    """The strike water helper with BrewStart waiting for its time (or none, brewing by hand), looking at the brew
    steps' basic data as it is at each call."""
    helper = strike_helper(tmp_path)
    helper.cbpi.step.basic_data = dict(basic or {})
    bs = Step("bs", "BrewStart", type="BrewStart", status=StepState.ACTIVE,
              props=Props({"Timing": "Ready by", "Grain_Temp": grain_temp}))

    def look():
        place = helper.cbpi.step.basic_data.get("grain_place")
        return types.SimpleNamespace(ids=["bs"], name="v5", unit="F", bs=bs if brewstart else None,
                                     inputs={"typed": None if grain_temp == "" else grain_temp, "grain_place": place})

    helper.update = look
    helper.get_state = lambda look=None: {}
    return helper, bs


def answer_grain(helper, **body):
    return asyncio.run(helper.set_grain(dict(body, steps=["bs"])))["applied"]


def test_a_grain_temperature_is_an_answer_indoors(tmp_path):
    helper, bs = grain_answer(tmp_path)
    applied = answer_grain(helper, grain=70)
    assert helper.cbpi.step.basic_data["grain_place"] == "indoors" and applied["place"] == "indoors"
    assert bs.props["Grain_Temp"] == 70.0
    helper.cbpi.step.save.assert_awaited()


def test_the_garage_is_answered_with_null_and_place_even_when_grain_temp_is_already_blank(tmp_path):
    helper, bs = grain_answer(tmp_path)
    applied = answer_grain(helper, grain=None, place="garage")
    assert helper.cbpi.step.basic_data["grain_place"] == "garage" and applied["place"] == "garage"
    assert bs.props["Grain_Temp"] == ""


@pytest.mark.parametrize("before, after", [(None, None), ("indoors", None), ("garage", "garage")])
def test_null_without_a_place_answers_nothing(tmp_path, before, after):
    helper, bs = grain_answer(tmp_path, {} if before is None else {"grain_place": before},
                              grain_temp=64.0 if before == "indoors" else "")
    answer_grain(helper, grain=None)
    assert helper.cbpi.step.basic_data.get("grain_place") == after and bs.props["Grain_Temp"] == ""


def test_brewing_by_hand_the_answer_is_kept_with_the_steps_too(tmp_path):
    helper, _ = grain_answer(tmp_path, brewstart=False)
    answer_grain(helper, grain=None, place="garage")
    assert helper.cbpi.step.basic_data["grain_place"] == "garage" and helper.day["grain"] is None
    helper.cbpi.step.save.assert_awaited_once()


@pytest.mark.parametrize("body", [dict(grain=64, place="garage"), dict(grain=None, place="indoors"),
                                  dict(grain=None, place="attic"), dict(grain=None, place="")])
def test_a_place_that_doesnt_fit_the_figure_is_refused(tmp_path, body):
    from cbpi.controller.strike_controller import StrikeRefused
    helper, bs = grain_answer(tmp_path, {"grain_place": "garage"})
    with pytest.raises(StrikeRefused) as refused:
        answer_grain(helper, **body)
    assert (refused.value.status, refused.value.error) == (400, "bad_body")
    assert helper.cbpi.step.basic_data == {"grain_place": "garage"} and bs.props["Grain_Temp"] == ""
    helper.cbpi.step.save.assert_not_awaited()
