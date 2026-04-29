"""Centralized connection pool budget.

Reads ``MEDANON_GLOBAL_MAX_CONNECTIONS`` (default 100) and derives per-subsystem
budgets as proportional allocations.  Each subsystem's own env-var override
(e.g. ``GPAS_POOL_SIZE``) takes precedence over the budget-derived default.

Budget allocation (when no per-subsystem override)::

    gPAS HTTP      30%  →  30 connections  (GPAS_POOL_SIZE)
    FHIR HTTP      15%  →  15 connections  (FHIR_POOL_SIZE)
    Proxy HTTP     15%  →  15 connections  (PROXY_POOL_SIZE)  — shared NLP + Analytics
    PG shared      25%  →  25 connections  (PG_POOL_MAX)
    PG staging     15%  →  15 connections  (PG_STAGING_POOL_MAX)
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
