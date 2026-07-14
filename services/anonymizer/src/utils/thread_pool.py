"""Process-wide bounded thread pool.

All modules that need concurrent I/O or CPU work should use ``get_executor()``
instead of creating per-call ``ThreadPoolExecutor`` instances.  A single shared
pool with a hard cap prevents thread explosion when multiple jobs run
concurrently.

Usage::

    from utils.thread_pool import get_executor
    from concurrent.futures import as_completed

    futures = {get_executor().submit(fn, arg): arg for arg in items}
    for fut in as_completed(futures, timeout=300):
        result = fut.result()

The pool is created lazily on first access and lives for the process lifetime.
Never call ``shutdown()`` on the returned executor — it is shared.
"""

from __future__ import annotations

import contextvars
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor

# Total thread budget across all integrations (gPAS sub-batches, FHIR fetch,
# upload, bulk download, Pass 1 parallelism, etc.).  Default 64 supports
# concurrent jobs with parallel Pass 1 (8-16 threads each) plus headroom.
# Override via MEDANON_GLOBAL_MAX_THREADS.
_MAX_THREADS: int = int(os.environ.get("MEDANON_GLOBAL_MAX_THREADS", "64"))

# Maximum pending tasks beyond the active workers before submit() blocks.
# Default: 1× max_workers (was 2× — a deeper queue absorbed bursts but also
# stockpiled requests during upstream degradation, then released them all
# at once on recovery, causing thundering-herd spikes against gPAS/NLP).
# A 1× queue keeps the in-flight + pending budget at 2× workers, which is
# enough to smooth normal scheduling jitter without amplifying cascades.
# Override via MEDANON_POOL_QUEUE_DEPTH if a deeper queue is desired.
_QUEUE_DEPTH: int = int(os.environ.get("MEDANON_POOL_QUEUE_DEPTH", str(_MAX_THREADS)))

# How long submit() waits for a semaphore slot before raising TimeoutError.
# Default 5 s gives upstream services a chance to drain a brief spike but
# fails fast under sustained saturation — holding the caller for longer
# (the previous 60 s default) just causes HTTP clients to time out and
# retry, amplifying the load on a degraded upstream.  Override via
# MEDANON_POOL_SUBMIT_TIMEOUT (legacy installs may want the older 60 s).
_SUBMIT_TIMEOUT_SEC: float = float(os.environ.get("MEDANON_POOL_SUBMIT_TIMEOUT", "5"))

_executor: "_BoundedExecutor | None" = None
_executor_lock = threading.Lock()


class _BoundedExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor with backpressure via a BoundedSemaphore.

    ``submit()`` blocks when ``max_workers + queue_depth`` tasks are already
    in-flight or queued, preventing unbounded memory growth under sustained
    overload (e.g. gPAS degradation with slow draining).

    If a semaphore slot is not available within ``_SUBMIT_TIMEOUT_SEC``,
    ``submit()`` raises ``TimeoutError`` so callers fail fast instead of
    blocking indefinitely.
    """

    def __init__(self, max_workers: int, queue_depth: int, **kwargs):
        super().__init__(max_workers, **kwargs)
        # Semaphore capacity = workers + extra queue slots
        self._semaphore = threading.BoundedSemaphore(max_workers + queue_depth)

    def submit(self, fn, *args, **kwargs):  # type: ignore[override]
        acquired = self._semaphore.acquire(timeout=_SUBMIT_TIMEOUT_SEC)
        if not acquired:
            raise TimeoutError(
                f"Thread pool saturated: could not acquire a slot within "
                f"{_SUBMIT_TIMEOUT_SEC}s — upstream service may be degraded"
            )
        try:
            future = super().submit(fn, *args, **kwargs)
        except Exception:
            self._semaphore.release()
            raise
        future.add_done_callback(lambda _: self._semaphore.release())
        return future


def submit_with_context(pool: ThreadPoolExecutor, fn, *args, **kwargs) -> Future:
    """Submit *fn* to *pool*, preserving the caller's ``contextvars``.

    ``ThreadPoolExecutor.submit`` does **not** copy the calling thread's
    ``contextvars.Context`` into the worker thread (unlike ``asyncio.to_thread``
    / ``loop.run_in_executor``, which do). Any contextvar the caller has set —
    the correlation id (:mod:`pipeline.trace`), the active data-permit id
    (:mod:`pipeline.permit_context`) — would silently reset to its default
    inside the worker thread, which is a correctness bug for permit-scoped
    pseudonymisation keys/domains (they would derive as *unscoped*, or raise
    the regulated-mode "no permit" error, depending on context).

    Use this instead of ``pool.submit`` wherever the submitted work reads a
    contextvar (directly, or transitively via ``cryptohash``/``tokenize``/
    ``date_shift``/``gpas_orchestrator``).
    """
    ctx = contextvars.copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)


def get_executor() -> _BoundedExecutor:
    """Return (lazily creating) the process-wide bounded thread pool.

    Thread-safe via double-check locking — prevents orphaned pools.
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = _BoundedExecutor(
                    max_workers=_MAX_THREADS,
                    queue_depth=_QUEUE_DEPTH,
                    thread_name_prefix="medanon",
                )
    return _executor
