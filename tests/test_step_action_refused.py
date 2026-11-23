"""POST /step2/action/{id}: a step's own reason when its action says no (4.7.7.post61).

A step says no by raising ValueError with its reason, in the brewer's language, as BrewStart's change_time does for a
time that has passed. The endpoint answers 400 {"error": "action refused", "detail": <the reason>}, and the reason is
logged as an answer, not a fault. Every other way an action doesn't run is answered as before, "action not run" and
its sentence: no such step, an action the step doesn't declare, a ValueError with no words, and any other exception.
"""

import asyncio
import json
import logging
from unittest import mock

import pytest

from cbpi.api.dataclasses import Step
from cbpi.api.decorator import action
from cbpi.controller.step_controller import StepController
from cbpi.http_endpoints.http_step import StepHttpEndpoints

REASON = ("Sun Nov 22, 00:00 has already passed, so BrewStart's time didn't change. For tomorrow morning, choose "
          "Tomorrow.")
NOT_RUN = "'{}' did not run. Either the step is not running, or that is not a declared action."


class Asks:
    """A step type whose actions run, say no, or fail."""

    def __init__(self):
        self.summary, self.summary2 = "", None
        self.ran = []

    @action("Works", [])
    async def works(self, **kwargs):
        self.ran.append("works")

    @action("Says no", [])
    async def says_no(self, **kwargs):
        self.ran.append("says_no")
        raise ValueError(REASON)

    @action("Says no without words", [])
    async def says_no_quietly(self, **kwargs):
        self.ran.append("says_no_quietly")
        raise ValueError()

    @action("Breaks", [])
    async def breaks(self, **kwargs):
        self.ran.append("breaks")
        raise RuntimeError("a bug in the step")

    async def not_declared(self, **kwargs):
        self.ran.append("not_declared")


def brew():
    """Brew steps on a stand-in: one step, "bs", of the type above."""
    controller = StepController.__new__(StepController)
    controller.profile = [Step("bs", "BrewStart", type="Asks", instance=Asks())]
    controller.cbpi = mock.Mock()
    return controller


class Request:
    """A POST to /step2/action/{id}."""

    def __init__(self, step_id, name):
        self.match_info = {"id": step_id}
        self._body = {"action": name, "parameter": {}}

    async def json(self):
        return self._body


def post(controller, step_id, name):
    """-> (status, body) of the action endpoint."""
    ep = StepHttpEndpoints.__new__(StepHttpEndpoints)
    ep.cbpi, ep.controller = controller.cbpi, controller
    response = asyncio.run(ep.http_call_action(Request(step_id, name)))
    return response.status, json.loads(response.body) if response.body else None


def test_a_step_that_says_no_is_answered_with_its_own_reason():
    steps = brew()
    assert post(steps, "bs", "says_no") == (400, {"error": "action refused", "detail": REASON})
    assert steps.profile[0].instance.ran == ["says_no"]


@pytest.mark.parametrize("step_id, name", [("gone", "works"), ("bs", "not_declared"), ("bs", "says_no_quietly"),
                                           ("bs", "breaks")])
def test_every_other_way_an_action_doesnt_run_is_answered_as_before(step_id, name):
    assert post(brew(), step_id, name) == (400, {"error": "action not run", "detail": NOT_RUN.format(name)})


def test_an_action_that_runs_is_answered_200_and_call_action_still_says_true_or_false():
    steps = brew()
    assert post(steps, "bs", "works") == (200, None)
    assert asyncio.run(steps.call_action("bs", "works", {})) is True
    assert asyncio.run(steps.call_action("bs", "says_no", {})) is False


def test_the_reason_is_logged_as_an_answer_not_a_fault(caplog):
    with caplog.at_level(logging.DEBUG):
        post(brew(), "bs", "says_no")
    told = [r for r in caplog.records if REASON in r.getMessage()]
    assert told and all(r.levelno == logging.INFO and not r.exc_info for r in told)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
