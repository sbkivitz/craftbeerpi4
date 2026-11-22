import asyncio
import json
from unittest import mock

import pytest

from cbpi.api.dataclasses import Props, Step, StepState
from cbpi.controller import step_controller
from cbpi.controller.step_controller import StepController
from cbpi.http_endpoints.http_step import StepHttpEndpoints

# Looked up, not imported, so on a core without it each test fails on its own.
OrderRefused = getattr(step_controller, "OrderRefused", None)
BEGUN = "{} has begun, so it can't move, and no step can go above it."


class Plain:
    """A step type that does nothing."""

    def __init__(self, cbpi, id, name, props, on_done):
        self.id, self.name, self.props = id, name, props
        self.summary, self.summary2 = "", None


def brew(*steps):
    """Brew steps on a stand-in: (name, status) pairs, or names for steps still to come."""
    controller = StepController.__new__(StepController)
    controller.profile = []
    for step in steps:
        name, status = (step, StepState.INITIAL) if isinstance(step, str) else step
        controller.profile.append(Step(name.lower().replace(" ", "-"), name, status=status))
    controller.types = {"Plain": {"class": Plain}}
    controller.cbpi = mock.Mock()
    controller.logger = mock.Mock()
    controller.save = mock.AsyncMock()
    controller.push_udpate = mock.Mock()
    return controller


def names(controller):
    return [step.name for step in controller.profile]


def ids(controller):
    return [step.id for step in controller.profile]


def refused(call):
    with pytest.raises(OrderRefused or Exception) as caught:
        asyncio.run(call)
    return caught.value


def test_the_whole_order_in_one_change_saved_once():
    steps = brew("MashIn", "Protein Rest", "Mash Out", "Boil", "BrewStart")
    assert asyncio.run(steps.reorder(["brewstart", "mashin", "protein-rest", "mash-out", "boil"])) is True
    assert names(steps) == ["BrewStart", "MashIn", "Protein Rest", "Mash Out", "Boil"]
    steps.save.assert_awaited_once()


def test_the_same_order_changes_and_saves_nothing():
    steps = brew("MashIn", "Boil")
    assert asyncio.run(steps.reorder(["mashin", "boil"])) is False
    steps.save.assert_not_called()


@pytest.mark.parametrize("given", [["mashin"], ["mashin", "boil", "other"], ["mashin", "mashin"],
                                   ["mashin", "other"], []])
def test_ids_not_exactly_the_loaded_ones_are_refused_with_them(given):
    steps = brew("MashIn", "Boil")
    error = refused(steps.reorder(given))
    assert (error.status, error.body()) == (409, {"error": "steps_changed", "steps": ["mashin", "boil"]})
    assert names(steps) == ["MashIn", "Boil"]
    steps.save.assert_not_called()


@pytest.mark.parametrize("given", [None, "mashin", ["mashin", 1], {"ids": ["mashin"]}])
def test_ids_that_arent_a_list_of_strings_are_a_bad_body(given):
    steps = brew("MashIn", "Boil")
    error = refused(steps.reorder(given))
    assert (error.status, error.error) == (400, "bad_body")
    steps.save.assert_not_called()


@pytest.mark.parametrize("given", [
    ["mashin", "boil", "protein-rest", "mash-out"],   # a step to come above the stopped rest
    ["protein-rest", "mashin", "mash-out", "boil"],   # the two begun steps swapped
    ["mashin", "mash-out", "protein-rest", "boil"],   # the stopped rest moved down
])
def test_a_step_that_has_begun_keeps_its_place_and_nothing_goes_above_it(given):
    steps = brew(("MashIn", StepState.DONE), ("Protein Rest", StepState.STOP), "Mash Out", "Boil")
    error = refused(steps.reorder(given))
    assert (error.status, error.body()) == (409, {"error": "begun", "detail": BEGUN.format("Protein Rest"),
                                                  "step": "Protein Rest"})
    assert names(steps) == ["MashIn", "Protein Rest", "Mash Out", "Boil"]
    steps.save.assert_not_called()


@pytest.mark.parametrize("status", [StepState.ACTIVE, StepState.STOP, StepState.ERROR, StepState.DONE])
def test_the_steps_after_the_last_begun_one_can_be_put_in_any_order(status):
    steps = brew(("MashIn", StepState.DONE), ("Protein Rest", status), "Mash Out", "Boil", "Whirlpool")
    assert asyncio.run(steps.reorder(["mashin", "protein-rest", "whirlpool", "mash-out", "boil"])) is True
    assert names(steps) == ["MashIn", "Protein Rest", "Whirlpool", "Mash Out", "Boil"]
    steps.save.assert_awaited_once()


def test_a_step_is_added_at_its_index():
    steps = brew("MashIn", "Boil")
    added = asyncio.run(steps.add(Step(name="BrewStart", props=Props(), type="Plain"), index=0))
    assert names(steps) == ["BrewStart", "MashIn", "Boil"]
    assert added.status == StepState.INITIAL and steps.profile[0] is added and isinstance(added.instance, Plain)
    asyncio.run(steps.add(Step(name="Cooldown", props=Props(), type="Plain"), index=3))
    asyncio.run(steps.add(Step(name="Whirlpool", props=Props(), type="Plain")))
    assert names(steps) == ["BrewStart", "MashIn", "Boil", "Cooldown", "Whirlpool"]
    assert steps.save.await_count == 3


@pytest.mark.parametrize("index", [-1, 3, 1.0, "1", True])
def test_an_index_that_isnt_a_whole_number_from_0_to_the_number_of_steps_is_a_bad_body(index):
    steps = brew("MashIn", "Boil")
    error = refused(steps.add(Step(name="Rest", props=Props(), type="Plain"), index=index))
    assert (error.status, error.error) == (400, "bad_body")
    assert names(steps) == ["MashIn", "Boil"]
    steps.save.assert_not_called()


def test_no_step_is_added_at_or_above_a_step_that_has_begun():
    steps = brew(("MashIn", StepState.DONE), ("Protein Rest", StepState.ACTIVE), "Boil")
    for index in (0, 1):
        error = refused(steps.add(Step(name="Rest", props=Props(), type="Plain"), index=index))
        assert (error.status, error.body()) == (409, {"error": "begun", "detail": BEGUN.format("Protein Rest"),
                                                      "step": "Protein Rest"})
    assert names(steps) == ["MashIn", "Protein Rest", "Boil"]
    asyncio.run(steps.add(Step(name="Mash Out", props=Props(), type="Plain"), index=2))
    assert names(steps) == ["MashIn", "Protein Rest", "Mash Out", "Boil"]


def endpoints(steps):
    ep = StepHttpEndpoints.__new__(StepHttpEndpoints)
    ep.cbpi, ep.controller = steps.cbpi, steps
    return ep


def request_with(body):
    request = mock.Mock()
    if isinstance(body, Exception):
        request.json = mock.AsyncMock(side_effect=body)
    else:
        request.json = mock.AsyncMock(return_value=body)
    return request


def test_put_order_answers_with_the_order_or_why_not():
    steps = brew(("MashIn", StepState.DONE), "Boil", "Whirlpool")
    ep = endpoints(steps)
    res = asyncio.run(ep.http_order(request_with({"ids": ["mashin", "whirlpool", "boil"]})))
    assert (res.status, json.loads(res.text)) == (200, {"changed": True, "steps": ["mashin", "whirlpool", "boil"]})
    res = asyncio.run(ep.http_order(request_with({"ids": ["mashin", "whirlpool", "boil"]})))
    assert (res.status, json.loads(res.text)) == (200, {"changed": False, "steps": ["mashin", "whirlpool", "boil"]})
    res = asyncio.run(ep.http_order(request_with({"ids": ["whirlpool", "mashin", "boil"]})))
    assert (res.status, json.loads(res.text)) == (409, {"error": "begun", "detail": BEGUN.format("MashIn"),
                                                        "step": "MashIn"})
    res = asyncio.run(ep.http_order(request_with({"ids": ["mashin", "boil"]})))
    assert (res.status, json.loads(res.text)) == (409, {"error": "steps_changed",
                                                        "steps": ["mashin", "whirlpool", "boil"]})
    for body in (ValueError("not JSON"), ["mashin"], {"ids": "mashin"}, {}):
        res = asyncio.run(ep.http_order(request_with(body)))
        assert (res.status, json.loads(res.text)["error"]) == (400, "bad_body")
    assert ids(steps) == ["mashin", "whirlpool", "boil"]
    steps.save.assert_awaited_once()


def test_post_puts_a_step_at_its_index_or_says_why_not():
    steps = brew(("MashIn", StepState.DONE), "Boil")
    ep = endpoints(steps)
    res = asyncio.run(ep.http_add(request_with({"name": "Rest", "type": "Plain", "props": {}, "index": 1})))
    assert res.status == 200 and json.loads(res.text)["name"] == "Rest"
    res = asyncio.run(ep.http_add(request_with({"name": "BrewStart", "type": "Plain", "props": {}, "index": 0})))
    assert (res.status, json.loads(res.text)) == (409, {"error": "begun", "detail": BEGUN.format("MashIn"),
                                                        "step": "MashIn"})
    res = asyncio.run(ep.http_add(request_with({"name": "Late", "type": "Plain", "props": {}, "index": 9})))
    assert (res.status, json.loads(res.text)["error"]) == (400, "bad_body")
    res = asyncio.run(ep.http_add(request_with({"name": "Last", "type": "Plain", "props": {}, "index": None})))
    assert res.status == 200
    assert names(steps) == ["MashIn", "Rest", "Boil", "Last"]
