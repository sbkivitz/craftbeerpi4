"""asyncio helpers for code that has to keep control of hardware."""

import asyncio


async def bounded(awaitable, seconds):
    """Await `awaitable`, giving up after `seconds` with TimeoutError.

    asyncio.timeout where there is one (3.11+): it runs the awaitable in the
    current task. 3.11's wait_for runs it in a new task instead, so code
    inside it - an actor driver - no longer looked like it was running in the
    logic's own task, which is what stop() checks to tell a logic stopping
    itself from one being stopped (DEFECTS F23).
    """
    timeout = getattr(asyncio, "timeout", None)
    if timeout is None:
        return await asyncio.wait_for(awaitable, seconds)
    async with timeout(seconds):
        return await awaitable


def cancellation_is_real():
    """Is the CancelledError being handled a cancellation of the CURRENT task?

    An awaited operation can raise CancelledError without anyone having
    cancelled the task awaiting it - a driver awaiting something that was
    cancelled. Taking that for a cancellation ended fermenter control for good
    (DEFECTS F20), and, inside a stop's own cleanup, abandoned the switch-off
    of every actor after it (F28). Python 3.11+ counts the cancel requests
    made of a task; before that there is no way to tell, and a cancellation
    is honoured.
    """
    cancelling = getattr(asyncio.current_task(), "cancelling", None)
    return True if cancelling is None else cancelling() > 0
