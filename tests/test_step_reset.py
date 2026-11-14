import asyncio
import json
from unittest import mock

from cbpi.api.dataclasses import Step, StepState
from cbpi.controller.step_controller import StepController
from cbpi.http_endpoints.http_step import StepHttpEndpoints


def endpoints(controller):
    ep = StepHttpEndpoints.__new__(StepHttpEndpoints)
    ep.controller = controller
    return ep


def test_reset_while_a_step_runs_is_refused_with_409_and_why():
    steps = StepController.__new__(StepController)
    steps.profile = [Step("a", "MashIn", status=StepState.DONE), Step("b", "Rest", status=StepState.ACTIVE),
                     Step("c", "Boil", status=StepState.INITIAL)]
    steps.save = mock.AsyncMock()
    res = asyncio.run(endpoints(steps).http_reset(None))
    assert res.status == 409
    assert "running" in json.loads(res.text)["error"]
    assert [s.status for s in steps.profile] == [StepState.DONE, StepState.ACTIVE, StepState.INITIAL]
    steps.save.assert_not_called()
    # a reset done is still 200
    done = mock.Mock()
    done.reset_all = mock.AsyncMock(return_value=True)
    assert asyncio.run(endpoints(done).http_reset(None)).status == 200
