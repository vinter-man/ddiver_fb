"""Awaiting a browser call that may never answer.

``asyncio.wait_for`` cancels the call at its timeout and then *waits for it to unwind*. A
Playwright call unwinds by asking its driver process to abort and awaiting that answer, so when
the driver process itself is wedged, ``wait_for`` never returns.
Here the call runs as a task instead: past the deadline it is cancelled, given a grace to
unwind, and then left behind, and the caller goes on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any


def forget(task: asyncio.Task[Any]) -> None:
    """Take the end of a task nobody awaits any more, so asyncio does not warn about it."""
    if not task.cancelled():
        task.exception()


async def abandon(task: asyncio.Task[Any], grace: float = 0.0) -> bool:
    """Cancel ``task`` and wait up to ``grace`` seconds for it to end; whether it did. One that
    did not is left behind (its end is taken by ``forget`` whenever it comes)."""
    task.add_done_callback(forget)
    task.cancel()
    if grace > 0:
        await asyncio.wait({task}, timeout=grace)
    return task.done()


async def run_within[T](
    coro: Coroutine[Any, Any, T], seconds: float, *, grace: float = 0.0, name: str | None = None
) -> T:
    """``coro``'s result, or ``TimeoutError`` after ``seconds`` with ``coro`` abandoned.

    A cancel of the caller abandons it the same way before the cancellation goes on.
    """
    task = asyncio.create_task(coro, name=name)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
    except asyncio.CancelledError:
        await abandon(task, grace)
        raise
    if done:
        return task.result()
    await abandon(task, grace)
    msg = f"no answer in {seconds:g} s"
    raise TimeoutError(msg)
