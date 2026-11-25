"""UPDATE or ADD AS A NEW RECIPE, for a BeerXML import whose name is in the Recipe Book already (4.7.7.post72).

A recipe in the book that lacks BeerSmith's mash figures - imported before they were kept, changed in the book, kept
with SAVE TO BOOK, or written by hand - gets them by importing its BeerSmith file again, without losing the brewer's
changes:

- UPDATE (POST /upload/xml {"id", "update": <book file>}) adds the file's mash figures and the file itself to that
  book recipe and keeps its steps, name, author and notes. One the brewer never changed, still as an import left it,
  is replaced by the file's recipe, as an import always replaced it. Where that book recipe is the one loaded on Brew
  Day, the brew steps take the figures and the file at once, whatever runs, with no step changed and nothing loaded
  again. A refusal changes nothing and says so.
- ADD AS A NEW RECIPE ({"id", "new": true}) keeps the import as a book recipe of its own, replacing nothing.
- Neither: as before, an earlier import of the name is replaced.

Each test fails on a core before post72.
"""

import asyncio
import copy
import json
import os
from unittest import mock

import pytest
import yaml

from cbpi.api.dataclasses import StepState
from cbpi.controller.recipe_controller import RecipeController, UpdateRefused
from cbpi.http_endpoints.http_upload import UploadHttpEndpoints
from tests.test_recipe_pages import Folder, brew_steps, in_book, uploader

NAME = "Craftbeerpi Test Recipe v6"
FIGURES = {"grain_kg": 9.07184, "boil_l": 87.064973, "lacks": []}
MINE = [{"name": "MashIn", "type": "MashInStep", "status": "I", "props": {"Temp": 126}},
        {"name": "My own rest", "type": "MashStep", "status": "I", "props": {"Temp": 150, "Timer": 45}}]
FILE_STEPS = [{"name": "Protein Rest", "type": "MashStep", "status": "I", "props": {"Temp": 126}}]


def book(tmp_path):
    cbpi = mock.Mock()
    cbpi.config_folder = Folder(tmp_path)
    return RecipeController(cbpi)


def kept(tmp_path, file_id):
    return yaml.safe_load((tmp_path / "recipes" / "{}.yaml".format(file_id)).read_text())


def the_import(name=NAME):
    return dict(basic=dict(name=name, author="Bench", mash_numbers=dict(FIGURES)), steps=[dict(s) for s in FILE_STEPS])


def changed_in_book(tmp_path, file_id="mine", name=NAME):
    """A book recipe the brewer changed: no import mark, his own steps, author and notes, no figures."""
    return in_book(tmp_path, file_id, {"basic": {"name": name, "author": "Me", "desc": "my notes"},
                                       "steps": [dict(s) for s in MINE]})


SOURCE = (b'<?xml version="1.0" encoding="UTF-8"?>\n<RECIPES>\n<RECIPE><NAME>v6</NAME></RECIPE>\n</RECIPES>\n', NAME)


@pytest.fixture(autouse=True)
def recipes_folder(tmp_path):
    (tmp_path / "recipes").mkdir(exist_ok=True)


# ------------------------------------------------------------------------------------------------- the book's side

def test_update_keeps_the_brewers_steps_and_notes_and_adds_the_figures_and_the_file(tmp_path):
    changed_in_book(tmp_path)
    done = asyncio.run(book(tmp_path).update_from_import("mine", the_import(), "BeerXML", source_xml=SOURCE))
    entry = kept(tmp_path, "mine")
    assert entry["steps"] == MINE
    assert entry["basic"] == {"name": NAME, "author": "Me", "desc": "my notes", "mash_numbers": FIGURES,
                              "source_xml": "recipes/mine.xml", "source_xml_recipe": NAME}
    assert (tmp_path / "recipes" / "mine.xml").read_bytes() == SOURCE[0]
    assert done["steps"] == "kept" and done["same_steps"] is True
    assert done["modified"] == os.path.getmtime(tmp_path / "recipes" / "mine.yaml")


def test_an_updated_recipe_stays_the_brewers_own_so_a_later_plain_import_adds_a_copy(tmp_path):
    changed_in_book(tmp_path)
    recipes = book(tmp_path)
    asyncio.run(recipes.update_from_import("mine", the_import(), "BeerXML"))
    assert "imported_from" not in kept(tmp_path, "mine")["basic"]
    file_id, replaced = asyncio.run(recipes.keep_import(the_import(), "BeerXML"))
    assert file_id != "mine" and replaced is False
    assert kept(tmp_path, "mine")["steps"] == MINE


def test_update_of_an_import_never_changed_takes_the_files_steps_as_an_import_always_did(tmp_path):
    in_book(tmp_path, "imp", {"basic": {"name": NAME, "author": "Bench", "imported_from": "BeerXML"},
                              "steps": [dict(s) for s in MINE]})
    done = asyncio.run(book(tmp_path).update_from_import("imp", the_import(), "BeerXML", source_xml=SOURCE))
    entry = kept(tmp_path, "imp")
    assert entry["steps"] == FILE_STEPS
    assert entry["basic"]["imported_from"] == "BeerXML" and entry["basic"]["mash_numbers"] == FIGURES
    assert entry["basic"]["source_xml"] == "recipes/imp.xml"
    assert done["steps"] == "file" and done["same_steps"] is False


def test_update_names_the_book_recipe_by_its_letters(tmp_path):
    changed_in_book(tmp_path, name="  craftbeerpi test RECIPE v6 ")
    asyncio.run(book(tmp_path).update_from_import("mine", the_import(), "BeerXML"))
    assert kept(tmp_path, "mine")["basic"]["name"] == "  craftbeerpi test RECIPE v6 "
    assert kept(tmp_path, "mine")["basic"]["mash_numbers"] == FIGURES


@pytest.mark.parametrize("file_id, words", [
    ("gone", RecipeController.UPDATE_GONE),
    ("../mine", RecipeController.UPDATE_GONE),
    ("other", "The Recipe Book's Rye Pale Ale isn't the file's Craftbeerpi Test Recipe v6, so it wasn't updated."),
])
def test_an_update_refused_changes_nothing_and_says_why(tmp_path, file_id, words):
    changed_in_book(tmp_path)
    in_book(tmp_path, "other", {"basic": {"name": "Rye Pale Ale"}, "steps": []})
    before = {p.name: p.read_bytes() for p in (tmp_path / "recipes").iterdir()}
    with pytest.raises(UpdateRefused) as refused:
        asyncio.run(book(tmp_path).update_from_import(file_id, the_import(), "BeerXML", source_xml=SOURCE))
    assert str(refused.value) == words
    assert {p.name: p.read_bytes() for p in (tmp_path / "recipes").iterdir()} == before


def test_add_as_a_new_recipe_never_replaces_an_earlier_import(tmp_path):
    in_book(tmp_path, "imp", {"basic": {"name": NAME, "imported_from": "BeerXML"}, "steps": [dict(s) for s in MINE]})
    file_id, replaced = asyncio.run(book(tmp_path).keep_import(the_import(), "BeerXML", new=True))
    assert file_id != "imp" and replaced is False
    assert kept(tmp_path, "imp")["steps"] == MINE
    assert kept(tmp_path, file_id)["steps"] == FILE_STEPS


# ----------------------------------------------------------------------------------------- the import and Brew Day

def importer(tmp_path, loaded_from="mine", status=StepState.ACTIVE, book_modified=None):
    """An import with a real Recipe Book on tmp_path, and brew steps loaded from `loaded_from`, their one step in
    `status` (a BrewStart waiting counts as running)."""
    upload, cbpi = uploader(held=(("Not every boil addition has a reminder", "7 additions"),))
    read = upload.xml_recipe_creation

    async def with_file(recipe_id):
        await read(recipe_id)
        upload._staged["source_xml"] = SOURCE

    upload.xml_recipe_creation = with_file
    upload.too_full = lambda numbers: [("The HLT is too small", "It holds 20 gal.")]
    steps = brew_steps(tmp_path, {"name": NAME, "book_file": loaded_from, "loaded_at": 1.0,
                                  "book_modified": book_modified})
    steps.profile[0].status = status
    cbpi.step = steps
    cbpi.recipe = RecipeController(steps.cbpi)
    return upload, cbpi, steps


def test_update_of_the_loaded_recipe_gives_brew_day_the_figures_at_once_while_a_step_runs(tmp_path):
    path = changed_in_book(tmp_path)
    upload, cbpi, steps = importer(tmp_path, book_modified=os.path.getmtime(path))
    before = copy.deepcopy([s.to_dict() for s in steps.profile])
    ok, answer = asyncio.run(upload.import_recipe("xml", "1", update="mine"))
    assert ok is True
    assert answer == {"name": NAME, "file": "mine", "replaced": False, "updated": True, "steps": "kept", "loaded": True,
                      "warnings": [{"title": "The HLT is too small", "text": "It holds 20 gal."}]}
    assert [s.to_dict() for s in steps.profile] == before
    assert steps.profile[0].status == StepState.ACTIVE
    assert steps.basic_data["mash_numbers"]["boil_l"] == 87.064973
    assert steps.basic_data["source_xml"] == "recipes/mine.xml" and steps.basic_data["source_xml_recipe"] == NAME
    assert steps.basic_data["book_modified"] == os.path.getmtime(path)
    assert steps.basic_data["book_file"] == "mine"
    steps.save.assert_awaited_once()
    sent = [c.args[0] for c in steps.cbpi.ws.send.call_args_list if c.args[0].get("topic") == "mash_profile_update"]
    assert sent and sent[-1]["data"]["basic"]["mash_numbers"]["boil_l"] == 87.064973
    said = [c.args[:2] for c in cbpi.notify.call_args_list]
    assert said[0] == ("BeerXML recipe updated", NAME + ". Updated in your Recipe Book with BeerSmith's mash figures "
                       "and its file. Your steps are as they were. Brew Day's copy has the mash figures too, and its "
                       "steps are as they were.")
    assert kept(tmp_path, "mine")["steps"] == MINE


def test_brew_steps_the_book_changed_since_they_were_loaded_still_count_as_older(tmp_path):
    path = changed_in_book(tmp_path)
    older = os.path.getmtime(path) - 60
    upload, _, steps = importer(tmp_path, book_modified=older)
    asyncio.run(upload.import_recipe("xml", "1", update="mine"))
    assert steps.basic_data["mash_numbers"] == FIGURES
    assert steps.basic_data["book_modified"] == older


def test_update_of_another_book_recipe_leaves_the_brew_steps_alone(tmp_path):
    changed_in_book(tmp_path)
    upload, _, steps = importer(tmp_path, loaded_from="something-else")
    ok, answer = asyncio.run(upload.import_recipe("xml", "1", update="mine"))
    assert ok is True and answer["loaded"] is False
    assert "mash_numbers" not in steps.basic_data
    steps.save.assert_not_awaited()


def test_update_of_an_import_never_changed_keeps_every_warning_and_says_so(tmp_path):
    in_book(tmp_path, "imp", {"basic": {"name": NAME, "imported_from": "BeerXML"}, "steps": [dict(s) for s in MINE]})
    upload, cbpi, _ = importer(tmp_path, loaded_from="imp")
    ok, answer = asyncio.run(upload.import_recipe("xml", "1", update="imp"))
    assert ok is True and answer["steps"] == "file" and answer["loaded"] is True
    assert [w["title"] for w in answer["warnings"]] == ["Not every boil addition has a reminder", "The HLT is too small"]
    assert "its steps, mash figures and file are this file's" in cbpi.notify.call_args_list[0].args[1]


@pytest.mark.parametrize("kind, kwargs, words", [
    ("xml", {"update": "gone"}, RecipeController.UPDATE_GONE + " Nothing was changed in the Recipe Book."),
    ("xml", {"update": "mine", "new": True}, "Choose UPDATE or ADD AS A NEW RECIPE, not both. Nothing was changed in "
                                             "the Recipe Book."),
    ("kbh", {"update": "mine"}, "Only a BeerXML file has BeerSmith's mash figures, so nothing was updated from this "
                                "Kleiner Brauhelfer recipe. Nothing was changed in the Recipe Book."),
])
def test_an_update_refused_answers_why_and_changes_nothing(tmp_path, kind, kwargs, words):
    changed_in_book(tmp_path)
    upload, cbpi, steps = importer(tmp_path)
    before = {p.name: p.read_bytes() for p in (tmp_path / "recipes").iterdir()}
    assert asyncio.run(upload.import_recipe(kind, "1", **kwargs)) == (False, words)
    assert {p.name: p.read_bytes() for p in (tmp_path / "recipes").iterdir()} == before
    assert "mash_numbers" not in steps.basic_data
    assert cbpi.notify.call_args_list[-1].args[:2] == ("Recipe not updated", words)


def test_an_update_the_settings_refuse_says_nothing_was_changed(tmp_path):
    changed_in_book(tmp_path)
    upload, _, steps = importer(tmp_path)
    upload._import_problem = mock.Mock(return_value="No mash kettle is set. Choose it in Settings, MASH_TUN.")
    assert asyncio.run(upload.import_recipe("xml", "1", update="mine")) == (
        False, "No mash kettle is set. Choose it in Settings, MASH_TUN. Nothing was changed in the Recipe Book.")
    assert kept(tmp_path, "mine")["steps"] == MINE and "mash_numbers" not in steps.basic_data


def test_add_as_a_new_recipe_from_the_import(tmp_path):
    upload, cbpi = uploader()
    ok, _ = asyncio.run(upload.import_recipe("xml", "1", new=True))
    assert ok is True
    assert cbpi.recipe.keep_import.await_args.kwargs.get("new") is True


@pytest.mark.parametrize("body, how", [
    ({"id": "1"}, {}),
    ({"id": "1", "update": "mine"}, {"update": "mine"}),
    ({"id": "1", "new": True}, {"new": True}),
    ({"id": "1", "new": False}, {}),
])
def test_the_endpoint_reads_which_way(body, how):
    assert UploadHttpEndpoints._how(body) == how


@pytest.mark.parametrize("body", [{"id": "1", "update": 3}, {"id": "1", "update": ""}, {"id": "1", "new": "yes"}])
def test_the_endpoint_refuses_a_way_it_cant_read(body):
    res = UploadHttpEndpoints._how(body)
    assert res.status == 400 and json.loads(res.text)["error"] == "recipe not imported"
