import asyncio
import json
from unittest import mock

import pytest
import yaml

from cbpi.api.dataclasses import NotificationType, Step, StepState
from cbpi.controller.recipe_controller import RecipeController
from cbpi.controller.step_controller import StepController, StepRunning
from cbpi.controller.upload_controller import UploadController
from cbpi.http_endpoints.http_recipe import RecipeHttpEndpoints
from cbpi.http_endpoints.http_step import StepHttpEndpoints

STATUSES = [StepState.DONE, StepState.ACTIVE, StepState.INITIAL]


def brewing(tmp_path, running=True):
    """Brew steps with MashIn done, Protein Rest running (or stopped) and the boil waiting, on a server stand-in."""
    steps = StepController.__new__(StepController)
    steps.profile = [Step("a", "MashIn", status=StepState.DONE),
                     Step("b", "Protein Rest", status=StepState.ACTIVE if running else StepState.STOP),
                     Step("c", "Boil", status=StepState.INITIAL)]
    steps.path = str(tmp_path / "mash_profile.json")
    steps.cbpi = mock.Mock()
    steps.cbpi.step = steps
    steps.shutdown = mock.AsyncMock()
    steps.load = mock.Mock()
    steps._retire_prompts = mock.Mock()
    steps.push_udpate = mock.Mock()
    steps._tell = mock.Mock()
    return steps


def book(tmp_path, steps):
    """The recipe book's RecipeController, with one other recipe in it, on the same stand-in."""
    other = tmp_path / "other.yaml"
    other.write_text(yaml.dump({"basic": {"name": "Other"},
                                "steps": [{"name": "Rest", "type": "MashStep", "props": {}}]}))
    steps.cbpi.config_folder.get_recipe_file_by_id.return_value = str(other)
    recipes = RecipeController.__new__(RecipeController)
    recipes.cbpi = steps.cbpi
    ep = RecipeHttpEndpoints.__new__(RecipeHttpEndpoints)
    ep.cbpi, ep.controller = steps.cbpi, recipes
    request = mock.Mock()
    request.match_info = {"name": "other"}
    return ep, request


def unchanged(steps, tmp_path):
    assert [s.status for s in steps.profile] == STATUSES
    steps.shutdown.assert_not_called()
    assert not (tmp_path / "mash_profile.json").exists()


def test_loading_a_recipe_from_the_book_while_a_step_runs_answers_409_and_changes_nothing(tmp_path):
    steps = brewing(tmp_path)
    ep, request = book(tmp_path, steps)
    res = asyncio.run(ep.http_brew(request))
    assert res.status == 409
    body = json.loads(res.text)
    assert body == {"error": "step_running", "step": "Protein Rest",
                    "detail": "Protein Rest is running. Stop it before loading another recipe."}
    unchanged(steps, tmp_path)
    steps.cbpi.notify.assert_called_once_with("Recipe not loaded", body["detail"], NotificationType.INFO)


def test_loading_a_recipe_once_nothing_runs_still_loads_it(tmp_path):
    steps = brewing(tmp_path, running=False)
    ep, request = book(tmp_path, steps)
    assert asyncio.run(ep.http_brew(request)).status == 200
    steps.shutdown.assert_awaited_once()
    written = json.loads((tmp_path / "mash_profile.json").read_text())
    assert written["basic"]["name"] == "Other"
    assert [s["status"] for s in written["steps"]] == ["I"]


def test_clearing_the_brew_steps_while_a_step_runs_answers_409_and_changes_nothing(tmp_path):
    steps = brewing(tmp_path)
    ep = StepHttpEndpoints.__new__(StepHttpEndpoints)
    ep.controller = steps
    res = asyncio.run(ep.http_clear(None))
    assert res.status == 409
    assert json.loads(res.text) == {"error": "step_running", "step": "Protein Rest",
                                    "detail": "Protein Rest is running. Stop it before unloading the recipe."}
    unchanged(steps, tmp_path)
    steps.cbpi.notify.assert_called_once_with(
        "Recipe not unloaded", "Protein Rest is running. Stop it before unloading the recipe.",
        NotificationType.INFO)


def test_clearing_once_nothing_runs_still_clears(tmp_path):
    steps = brewing(tmp_path, running=False)
    ep = StepHttpEndpoints.__new__(StepHttpEndpoints)
    ep.controller = steps
    assert asyncio.run(ep.http_clear(None)).status == 200
    assert json.loads((tmp_path / "mash_profile.json").read_text()) == {"basic": {}, "steps": []}


def test_a_running_step_without_a_name_is_called_a_step(tmp_path):
    steps = brewing(tmp_path)
    steps.profile[1].name = ""
    with pytest.raises(StepRunning) as refused:
        asyncio.run(steps.load_recipe({"basic": {}, "steps": []}))
    assert refused.value.detail == "A step is running. Stop it before loading another recipe."


def test_an_import_while_a_step_runs_is_refused_before_the_recipe_is_read(tmp_path):
    steps = brewing(tmp_path)
    upload = UploadController.__new__(UploadController)
    upload.cbpi = steps.cbpi
    upload._import_lock = asyncio.Lock()
    upload.get_config_values = mock.Mock()
    upload.xml_recipe_creation = mock.AsyncMock()
    ok, message = asyncio.run(upload.import_recipe("xml", "1"))
    assert ok is False
    assert message == ("Protein Rest is running. Stop it before importing a recipe: an import replaces the brew "
                       "steps. The brew steps are unchanged.")
    upload.get_config_values.assert_not_called()
    upload.xml_recipe_creation.assert_not_called()
    unchanged(steps, tmp_path)
    steps.cbpi.notify.assert_called_once_with("Recipe not imported", message, NotificationType.INFO)
