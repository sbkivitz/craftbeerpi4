"""Who is commanding an actor: the brewer, or a control logic or step.

An actor's `power` is the level it runs at now. Kettle logics change it every
control cycle - PIDBoil, PID_HERMS and BoilPower set the element's duty with
actor_set_power() - so it also ended up being the level a brewer got when he
next switched the element on by hand. Scott, 10 Nov 2026, heating strike water
by hand on the simulator after the HLT's AUTO had run: "I see the power is
capped at 25%. Why?" The 25 % was PIDBoil's last duty.

So the actor controller keeps the brewer's own level apart (Actor.manual_power)
and changes it only for commands that do not come through a logic or step.
Those go through CBPiBase's actor helpers, which run them inside automation().
A context variable rather than an argument: every task has its own, so a
logic's command can never mark a brewer's command made at the same moment, and
nothing that implements or fakes the actor controller has to accept anything
new.
"""

import contextvars

_AUTOMATION = contextvars.ContextVar("cbpi_actor_automation", default=False)


def by_automation() -> bool:
    """True while a logic or step is commanding an actor (see automation())."""
    return _AUTOMATION.get()


class automation:
    """`with automation():` around a command a logic or step makes."""

    def __enter__(self):
        self._token = _AUTOMATION.set(True)
        return self

    def __exit__(self, *exc):
        _AUTOMATION.reset(self._token)
        return False
