"""Centralized connection pool budget.

Reads ``MEDANON_GLOBAL_MAX_CONNECTIONS`` (default 10000) and derives per-subsystem
budgets as proportional allocations.  Each subsystem's own env-var override
(e.g. ``GPAS_POOL_SIZE``) takes precedence over the budget-derived default.

Budget allocation (when no per-subsystem override)::

    gPAS HTTP      30%  →  3000 connections  (GPAS_POOL_SIZE)
    FHIR HTTP      15%  →  1500 connections  (FHIR_POOL_SIZE)
    Proxy HTTP     15%  →  1500 connections  (PROXY_POOL_SIZE)  — shared NLP + Analytics
    PG shared      25%  →  2500 connections  (PG_POOL_MAX)
    PG staging     15%  →  1500 connections  (PG_STAGING_POOL_MAX)
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.pool_budget")

GLOBAL_MAX = int(os.environ.get("MEDANON_GLOBAL_MAX_CONNECTIONS", "10000"))

# Proportional weights (must sum to 100)
_WEIGHTS = {
    "gpas": 30,
    "fhir": 15,
    "proxy": 15,
    "pg": 25,
    "pg_staging": 15,
}


def _budget(weight_key: str, env_var: str, min_size: int = 2) -> int:
    """Return pool size: env-var override > proportional budget > min_size."""
    override = os.environ.get(env_var, "").strip()
    if override:
        return max(int(override), min_size)
    return max(int(GLOBAL_MAX * _WEIGHTS[weight_key] / 100), min_size)


def gpas_pool_budget() -> int:
    return _budget("gpas", "GPAS_POOL_SIZE", min_size=4)


def fhir_pool_budget() -> int:
    return _budget("fhir", "FHIR_POOL_SIZE", min_size=4)


def proxy_pool_budget() -> int:
    return _budget("proxy", "PROXY_POOL_SIZE", min_size=4)


def pg_pool_budget() -> int:
    return _budget("pg", "PG_POOL_MAX", min_size=5)


def pg_staging_budget() -> int:
    return _budget("pg_staging", "PG_STAGING_POOL_MAX", min_size=2)


def log_pool_budget() -> None:
    """Emit a single INFO line with the resolved pool allocations."""
    _log.info(
        "pool_budget global=%d gpas=%d fhir=%d proxy=%d pg=%d pg_staging=%d",
        GLOBAL_MAX,
        gpas_pool_budget(),
        fhir_pool_budget(),
        proxy_pool_budget(),
        pg_pool_budget(),
        pg_staging_budget(),
    )


def check_thread_budget() -> None:
    """Warn when the effective pipeline concurrency exceeds the thread cap.

    ``MEDANON_PIPELINE_WIDTH`` concurrent consumers each spawn up to
    ``MEDANON_PARALLEL_WORKERS`` threads.  Their product must not exceed
    ``MEDANON_GLOBAL_MAX_THREADS`` or the thread pool will stall and
    throughput will degrade under load.

    Emits a WARNING (not an error) so the process can still start — operators
    may have deliberately configured a larger thread pool on their host.
    """
    from utils.thread_pool import _MAX_THREADS

    pipeline_width = int(os.environ.get("MEDANON_PIPELINE_WIDTH", "1"))
    parallel_workers = int(os.environ.get("MEDANON_PARALLEL_WORKERS", "8"))
    product = pipeline_width * parallel_workers

    if product > _MAX_THREADS:
        _log.warning(
            "thread_budget_exceeded MEDANON_PIPELINE_WIDTH=%d × "
            "MEDANON_PARALLEL_WORKERS=%d = %d > MEDANON_GLOBAL_MAX_THREADS=%d — "
            "reduce MEDANON_PIPELINE_WIDTH or MEDANON_PARALLEL_WORKERS to avoid "
            "thread-pool starvation and deadlocks under load",
            pipeline_width,
            parallel_workers,
            product,
            _MAX_THREADS,
        )
    elif product > int(_MAX_THREADS * 0.75):
        _log.warning(
            "thread_budget_high MEDANON_PIPELINE_WIDTH=%d × "
            "MEDANON_PARALLEL_WORKERS=%d = %d (%.0f%% of %d-thread cap) — "
            "consider lowering concurrency or raising MEDANON_GLOBAL_MAX_THREADS",
            pipeline_width,
            parallel_workers,
            product,
            100.0 * product / _MAX_THREADS,
            _MAX_THREADS,
        )
    else:
        _log.info(
            "thread_budget_ok pipeline_width=%d parallel_workers=%d "
            "effective=%d / %d threads",
            pipeline_width,
            parallel_workers,
            product,
            _MAX_THREADS,
        )
