"""Correction stage  explicit, observable handling of resources that fail.

Historically a resource that could not be processed was emitted as one of two
*different* error-dict shapes:

  - ``processor.py``        → ``{"error": "...", "resourceType": "..."}``
  - ``executor_stream.py``  → ``{"error": "...", "resourceType": "...", "id": "..."}``

Downstream code keys off ``result.get("error")`` and ``result.get("resourceType")``
inconsistently, and there was no single, queryable record describing *what* was
quarantined and *why*.  This module provides one canonical
:func:`quarantine_record` builder so every producer emits the same shape.

The record is intentionally a superset of the historical shapes  it keeps the
exact ``error`` and ``resourceType`` keys every existing consumer reads, and
adds structured fields (``id``, ``stage``, ``error_type``, ``quarantined``) so a
correction/audit consumer can reconstruct what happened without parsing prose.
"""

from __future__ import annotations

from typing import Any

# Stable marker so consumers can distinguish a quarantined resource from a
# legitimately-empty one without prose matching.
QUARANTINE_MARKER = "__quarantined"


def quarantine_record(
    *,
    error: str,
    resource_type: str = "Unknown",
    resource_id: str | None = None,
    stage: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Build the one canonical quarantine record.

    Parameters
    ----------
    error:          short human-readable reason (PHI-free).
    resource_type:  the resource's ``resourceType`` (``"Unknown"`` if unknown).
    resource_id:    the resource ``id`` when known.
    stage:          which pipeline stage failed (``match`` / ``finalize`` / …).
    error_type:     the exception class name (e.g. ``"ValueError"``).

    The returned dict always carries the legacy ``error`` + ``resourceType``
    keys so existing consumers (the ``"error" not in r`` filter, the summary's
    ``record_error``) keep working unchanged.
    """
    record: dict[str, Any] = {
        "error": error,
        "resourceType": resource_type,
        QUARANTINE_MARKER: True,
    }
    if resource_id is not None:
        record["id"] = resource_id
    if stage is not None:
        record["stage"] = stage
    if error_type is not None:
        record["error_type"] = error_type

    # Single instrumentation point for every quarantine path. Labels are
    # bounded (stage names + exception type names), never raw/PHI values.
    from utils.metrics import QUARANTINE_TOTAL

    QUARANTINE_TOTAL.labels(
        stage=stage or "unknown", reason=error_type or "unknown"
    ).inc()
    return record


def is_quarantined(result: Any) -> bool:
    """True if *result* is a quarantine record produced by this module.

    Falls back to the legacy ``"error" in result`` heuristic so records emitted
    before the marker existed are still recognised.
    """
    if not isinstance(result, dict):
        return False
    return result.get(QUARANTINE_MARKER) is True or "error" in result
