import asyncio
from unittest import mock

from cbpi.api.dataclasses import Step, StepState
from cbpi.controller.step_controller import StepController


def brew(*names):
    steps = StepController.__new__(StepController)
    steps.profile = [Step(name.lower(), name, status=StepState.INITIAL) for name in names]
    steps.logger = mock.Mock()
    steps.save = mock.AsyncMock()
    steps.push_udpate = mock.Mock()
    return steps


def order(steps):
    return [step.name for step in steps.profile]


def test_a_step_swaps_with_its_neighbour():
    steps = brew("MashIn", "Rest", "Boil")
    asyncio.run(steps.move("rest", -1))
    assert order(steps) == ["Rest", "MashIn", "Boil"]
    asyncio.run(steps.move("rest", 1))
    assert order(steps) == ["MashIn", "Rest", "Boil"]


def test_nothing_moves_past_either_end():
    steps = brew("MashIn", "Rest", "Boil")
    asyncio.run(steps.move("mashin", -1))
    assert order(steps) == ["MashIn", "Rest", "Boil"], "the first step moved up must not swap with the last"
    asyncio.run(steps.move("boil", 1))
    asyncio.run(steps.move("not-a-step", 1))
    assert order(steps) == ["MashIn", "Rest", "Boil"]
    steps.save.assert_not_called()
