"""Trace layer  correlation IDs + machine-readable per-resource trace records.

Two complementary mechanisms, decoupled from the human-facing transformation
manifest (``pipeline/manifest.py``, which lives inside ``meta.tag`` of the FHIR
output and is capped at 200 chars by HAPI):

  - **Correlation ID**  a ``contextvar`` minted at the entry point and read
    wherever a span or log line wants to tie work back to one request/job.
    Because it is a ``contextvar`` it propagates to the gPAS caller thread and,
    via the Phase-1 ``run_with_current_context`` OTel wrapper, to the NLP
    daemon thread.  ``_stage_span`` stamps it onto every pipeline span.

  - **Machine-readable trace record**  an out-of-band, append-only per-resource
    record ``{correlation_id, resource_id, resource_type, stages: [...]}`` that a
    debugging/audit consumer can query without parsing prose or scraping
    ``meta.tag``.  Emission is opt-in (``MEDANON_TRACE_RECORDS_ENABLED``) and
    independent of ``MEDANON_MANIFEST_ENABLED`` so the two can be toggled
    separately.
"""

from __future__ import annotations

import contextvars
import os
import uuid

# Minted at the entry point; empty string means "no correlation context set".
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "medanon_correlation_id", default=""
)


def new_correlation_id() -> str:
    """Generate a fresh correlation id (URL-safe, short)."""
    return uuid.uuid4().hex


def set_correlation_id(cid: str) -> contextvars.Token:
    """Set the active correlation id; returns a token for :func:`reset_correlation_id`."""
    return _correlation_id.set(cid)


def reset_correlation_id(token: contextvars.Token) -> None:
    """Restore the previous correlation id (pair with :func:`set_correlation_id`)."""
    try:
        _correlation_id.reset(token)
    except (ValueError, LookupError):
        # Token from a different context (e.g. crossed a thread boundary)
        # best-effort reset; never raise from a trace helper.
        pass


def get_correlation_id() -> str:
    """Return the active correlation id, or ``""`` when none is set."""
    return _correlation_id.get()


class correlation_scope:
    """Context manager that mints (or adopts) a correlation id for its body.

    Usage::

        with correlation_scope() as cid:
            ...  # cid is active for everything called within
    """

    def __init__(self, cid: str | None = None) -> None:
        self._cid = cid or new_correlation_id()
        self._token: contextvars.Token | None = None

    def __enter__(self) -> str:
        self._token = set_correlation_id(self._cid)
        return self._cid

    def __exit__(self, *_exc) -> None:
        if self._token is not None:
            reset_correlation_id(self._token)


# ---------------------------------------------------------------------------
# Machine-readable per-resource trace record (out-of-band, opt-in)
# ---------------------------------------------------------------------------


def trace_records_enabled() -> bool:
    return os.environ.get("MEDANON_TRACE_RECORDS_ENABLED", "false").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def build_trace_record(
    resource: dict | None,
    *,
    stages: list[dict] | None = None,
    quarantined: bool = False,
) -> dict:
    """Build one machine-readable trace record for a processed resource.

    Kept deliberately small and JSON-serialisable.  ``stages`` is a list of
    ``{"stage": ..., "status": ..., ...}`` entries the caller assembles; this
    function just frames them with the correlation id and resource identity.
    The record is NOT embedded in the FHIR output  it is meant for an
    out-of-band sink (log stream, trace store).
    """
    rid = None
    rtype = "Unknown"
    if isinstance(resource, dict):
        rid = resource.get("id")
        rtype = resource.get("resourceType", "Unknown")
    return {
        "correlation_id": get_correlation_id(),
        "resource_id": rid,
        "resource_type": rtype,
        "quarantined": quarantined,
        "stages": stages or [],
    }
