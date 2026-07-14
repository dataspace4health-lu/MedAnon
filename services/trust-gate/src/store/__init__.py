"""Durable persistence for the standalone Trust Gate QC platform.

The gate is a provider-facing data-quality service: providers submit data, get a
Quality Passport back, and the platform retains the assessment history so quality
can be tracked, trended, and remediated over time. This package owns that state.

Backends mirror ``baseline.py`` (selected by env, see ``get_passport_store``):
  - none (default)            stateless; assess still works, nothing is retained.
  - ``TRUST_GATE_STORE_DB_URL``  Postgres, durable + cross-replica (use when scaled).
  - ``TRUST_GATE_STORE_DB``      SQLite path (single-instance / dev).

Persistence is best-effort: a store outage never blocks an assessment.
"""

from store.findings_store import (
    FindingsStore,
    derive_findings,
    get_findings_store,
    reset_findings_store,
)
from store.passport_store import PassportStore, get_passport_store, reset_passport_store

__all__ = [
    "PassportStore",
    "get_passport_store",
    "reset_passport_store",
    "FindingsStore",
    "get_findings_store",
    "reset_findings_store",
    "derive_findings",
]
