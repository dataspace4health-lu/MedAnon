"""Background task retention helpers.

`asyncio.create_task()` returns a Task that is referenced only weakly by the
event loop. If the caller discards the return value (the common
fire-and-forget pattern), CPython may garbage-collect the task mid-execution,
silently cancelling the work. PEP 0 recommends keeping a strong reference.

`retain_task()` adds the task to a module-level set and arranges for it to be
removed when it completes, preventing GC without leaking memory.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Coroutine, Set

_log = logging.getLogger("medanon.tasks")

# Module-level strong reference set. Tasks remain here until they finish.
_BACKGROUND_TASKS: Set[asyncio.Task[Any]] = set()


def retain_task(coro: Coroutine[Any, Any, Any] | Awaitable[Any], *, name: str | None = None) -> asyncio.Task[Any]:
    """Schedule *coro* with `asyncio.create_task` and keep a strong reference.

    The task is added to a module-level set and removed automatically when it
    completes. If the task raises, the exception is logged at WARNING level so
    fire-and-forget failures are observable.

    Returns the created Task; the caller may ignore the return value.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            _log.warning("background task failed name=%s err=%s", t.get_name(), exc)

    task.add_done_callback(_on_done)
    return task


def active_task_count() -> int:
    """Return the number of currently-retained background tasks (for diagnostics)."""
    return len(_BACKGROUND_TASKS)
