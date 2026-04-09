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

import os
import threading
from concurrent.futures import ThreadPoolExecutor

# Total thread budget across all integrations (gPAS sub-batches, FHIR fetch,
# upload, bulk download, etc.).  Default 32 comfortably supports 3 concurrent
# jobs with up to 8 sub-threads each plus headroom for health probes.
# Override via MEDANON_GLOBAL_MAX_THREADS.
_MAX_THREADS: int = int(os.environ.get("MEDANON_GLOBAL_MAX_THREADS", "32"))

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def get_executor() -> ThreadPoolExecutor:
    """Return (lazily creating) the process-wide thread pool.

    Thread-safe via double-check locking — prevents orphaned pools.
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_THREADS,
                    thread_name_prefix="medanon",
                )
    return _executor
