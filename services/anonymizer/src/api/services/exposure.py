"""Cumulative-exposure ledger singleton + assessment service (D7.2 §5.5.7).

Wraps the durable :class:`PostgresReleaseLedger` behind the same
``init_*_store`` / ``get_*_store`` idiom used by ``api.services.reports``. The
assessment is scoped to a permit and/or recipient — cumulative disclosure is
only meaningful within a governance scope, never globally.

The population fingerprint is a keyed one-way hash of the subject ids
(``MEDANON_HASH_KEY``); no ids are stored. When no key is configured the ledger
cannot fingerprint safely, so assessment degrades to a no-op rather than
persisting weakly-hashed data.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pipeline.cumulative_exposure import (
    assess_cumulative_exposure,
    fingerprint_population,
)

logger = logging.getLogger("medanon.exposure")

_store: Any = None


def init_release_ledger(*, store: Any = None) -> Any:
    """Install the process-wide release ledger (called once at startup)."""
    global _store
    _store = store
    return _store


def get_release_ledger() -> Any:
    """Return the active release ledger, or None when not configured."""
    return _store


def _fingerprint_key() -> str:
    return os.environ.get("MEDANON_HASH_KEY", "").strip()


def assess_and_record(
    *,
    subject_ids: list[str],
    permit_id: str | None,
    recipient: str | None,
    qi_signature: Any = "",
    release_id: str | None = None,
    record: bool = False,
) -> dict:
    """Assess a release's cumulative exposure and (optionally) record it.

    Returns the assessment dict (see ``assess_cumulative_exposure``) with an
    added ``recorded`` flag. Raises ``ValueError`` when neither permit nor
    recipient is supplied (the assessment scope) or when no hash key is set.
    """
    if not permit_id and not recipient:
        raise ValueError("permit_id or recipient is required to scope exposure")
    key = _fingerprint_key()
    if not key:
        raise ValueError(
            "MEDANON_HASH_KEY is required to fingerprint a release population"
        )

    fingerprint = fingerprint_population(subject_ids, key)

    store = _store
    priors: list[dict] = []
    if store is not None:
        priors = store.prior_releases(
            permit_id=permit_id,
            recipient=recipient,
            exclude_release_id=release_id,
        )

    result = assess_cumulative_exposure(fingerprint, priors, qi_signature=qi_signature)

    recorded = False
    if record and store is not None and release_id:
        from pipeline.cumulative_exposure import _signature

        store.record(
            release_id,
            fingerprint=fingerprint,
            permit_id=permit_id,
            recipient=recipient,
            qi_signature=_signature(qi_signature),
            record_count=len(fingerprint),
        )
        recorded = True

    result["recorded"] = recorded
    return result
