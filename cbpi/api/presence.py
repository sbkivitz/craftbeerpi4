"""Whether what runs now is a person's press: NEXT, a step's button, the answer to a question, a pump or AUTO pressed.

Some things may happen only when the brewer is there to see them. After BrewStart, or a MashIn on the HLT, holds the
strike water, the next step starts only on his NEXT or his answer to the step's question; and the mash tun's wort
pump starts only once he has said the water is in the mash tun (StepController.strike_move_confirmed). A step, a
kettle logic or a plugin can't tell those apart from what it does by itself, so the places a press comes in say so:
POST /step2/next and /step2/action, a notification's button, POST /actor/{id}/on and POST /kettle/{id}/on and
/toggle. Everything else is not a press: a step's own loop, a timer, a kettle logic, a listener, a restart.

A press belongs to the task that took it in, and to nothing it starts. A context variable is copied into every task
created while it is set, so a step's loop, a timer or a kettle logic started during a press would otherwise go on
"being pressed" for as long as it runs, and could move on or switch the pump by itself hours later. So the press
remembers its task, and pressed() answers only in that same task: in what that request awaits, never in a task it
started. A notification's button runs in a task of its own, which takes the press itself (press_in_task).
"""

import asyncio
import contextvars

_PRESS = contextvars.ContextVar("cbpi_press", default=None)


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def pressed():
    """What the person pressed - "next", "step action", "answer", "actor", "auto" - if what runs now is that press,
    else None."""
    entry = _PRESS.get()
    if entry is None:
        return None
    what, task = entry
    if task is None or task is not _current_task():
        return None
    return what


class press:
    """`with press("next"):` around the handling of a person's press, in the task that took it in."""

    def __init__(self, what):
        self.what = str(what)

    def __enter__(self):
        self._token = _PRESS.set((self.what, _current_task()))
        return self

    def __exit__(self, *exc):
        _PRESS.reset(self._token)
        return False


async def press_in_task(what, call):
    """Await `call()` as a person's press, in the task this runs in: for a press handled in a task of its own."""
    with press(what):
        return await call()
