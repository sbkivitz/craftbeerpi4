"""The recipe editor's SAVE in the Recipe Book (PUT /recipe/{file}, RecipeController.save) merges the basic data it
sends into the book's, never replacing it: what it doesn't send stays (the recipe's mash figures, its kept BeerSmith
file), and what it sends wins, so mash figures changed in the editor are kept. And a save is a quiet line in the log,
never an error carrying the whole recipe.
"""

import asyncio
import logging
from unittest import mock

import yaml

from cbpi.controller.recipe_controller import RecipeController
from cbpi.http_endpoints.http_recipe import RecipeHttpEndpoints
from tests.test_recipe_pages import Folder

NUMBERS = {"grain_kg": 9.07184, "infuse_l": 31.27, "for_grain_l": 28.39, "lacks": []}


def book_with(tmp_path, basic):
    cbpi = mock.Mock()
    cbpi.config_folder = Folder(tmp_path)
    (tmp_path / "recipes" / "v5.yaml").write_text(yaml.dump({"basic": basic, "steps": []}))
    return RecipeController(cbpi)


def kept(tmp_path):
    return yaml.safe_load((tmp_path / "recipes" / "v5.yaml").read_text())


BOOK = {"name": "Test Recipe v5", "author": "Bench", "desc": "", "mash_numbers": NUMBERS,
        "source_xml": "recipes/v5.xml", "source_xml_recipe": "Test Recipe v5", "imported_from": "BeerXML",
        "brewscribe": {"id": 7}}


def test_a_save_of_the_name_author_and_notes_only_keeps_the_rest(tmp_path):
    rc = book_with(tmp_path, dict(BOOK))
    asyncio.run(rc.save("v5", {"basic": {"name": "Test Recipe v5b", "author": "Me", "desc": "Notes"},
                               "steps": [{"name": "Rest", "type": "MashStep", "props": {}}]}))
    basic = kept(tmp_path)["basic"]
    assert (basic["name"], basic["author"], basic["desc"]) == ("Test Recipe v5b", "Me", "Notes")
    assert basic["mash_numbers"] == NUMBERS
    assert basic["brewscribe"] == {"id": 7}
    assert (basic["source_xml"], basic["source_xml_recipe"]) == ("recipes/v5.xml", "Test Recipe v5")
    assert "imported_from" not in basic, "saved by the brewer, it's never taken for an import"


def test_the_mash_figures_sent_are_the_ones_kept(tmp_path):
    rc = book_with(tmp_path, dict(BOOK))
    changed = dict(NUMBERS, infuse_l=28.0, typed=["water"], beersmith={"infuse_l": 31.27})
    asyncio.run(rc.save("v5", {"basic": {"name": "Test Recipe v5", "mash_numbers": changed}, "steps": []}))
    assert kept(tmp_path)["basic"]["mash_numbers"] == changed


def test_a_save_with_no_basic_data_keeps_the_books(tmp_path):
    rc = book_with(tmp_path, dict(BOOK))
    asyncio.run(rc.save("v5", {"steps": []}))
    basic = kept(tmp_path)["basic"]
    assert basic["name"] == "Test Recipe v5" and basic["mash_numbers"] == NUMBERS


def test_a_save_is_a_quiet_line_without_the_recipe(tmp_path, caplog):
    rc = book_with(tmp_path, dict(BOOK))
    endpoint = RecipeHttpEndpoints.__new__(RecipeHttpEndpoints)
    endpoint.controller = rc
    request = mock.Mock()
    request.match_info = {"name": "v5"}
    request.json = mock.AsyncMock(return_value={"basic": {"name": "Secret Stout"}, "steps": []})
    caplog.set_level(logging.DEBUG)
    answer = asyncio.run(RecipeHttpEndpoints.http_save.__wrapped__(endpoint, request)
                         if hasattr(RecipeHttpEndpoints.http_save, "__wrapped__")
                         else RecipeHttpEndpoints.http_save(endpoint, request))
    assert answer.status == 204
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "a save is no warning or error"
    assert not [r for r in caplog.records if "Secret Stout" in r.getMessage()], "the recipe isn't written to the log"
    assert kept(tmp_path)["basic"]["name"] == "Secret Stout"
