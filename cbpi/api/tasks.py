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


def _retrieve(task):
    # An abandoned task's exception is never awaited by anyone; reading it
    # here keeps asyncio from logging "Task exception was never retrieved".
    if not task.cancelled():
        task.exception()


def spawn(factory, holder=None):
    """factory() in a task of its own; `holder`, a set, keeps it while it runs."""
    task = asyncio.ensure_future(factory())
    task.add_done_callback(_retrieve)
    if holder is not None:
        holder.add(task)
        task.add_done_callback(holder.discard)
    return task


async def wait_within(task, seconds):
    """`task`'s result, or TimeoutError once `seconds` have passed - whatever it does.

    bounded() cancels at its deadline and then waits for the awaitable to
    finish: for ever, if the awaitable catches the cancellation and carries
    on. A driver with a bare `except:` around a retry loop does exactly that,
    and the fermenter loop that called it stopped there - no control, no
    alert (DEFECTS F46). This stops waiting at the deadline: the task is
    cancelled and left to end on its own.

    If the caller is cancelled first, so is the task, and it gets what is
    left of its time to unwind before the cancellation goes on - so the
    cleanup that follows does not race a cooperative driver's own.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except asyncio.CancelledError:
        task.cancel()
        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.wait({task}, timeout=remaining)
        raise
    if not done:
        task.cancel()
        raise asyncio.TimeoutError("still running after {}s".format(seconds))
    return task.result()


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
