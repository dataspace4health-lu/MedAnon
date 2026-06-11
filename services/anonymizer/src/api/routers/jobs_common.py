"""Shared dependencies for the async-job routers.

The job endpoints were split across :mod:`api.routers.jobs_submit` (POST
submissions) and :mod:`api.routers.jobs_manage` (list / lifecycle / detail).
This module holds what both need: the singleton :class:`JobService`, the
submit rate-limit, idempotency, and target-URL resolution helpers. Keeping
these here avoids a circular import between the two router modules.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel

from api.deps import _validate_server_url
from api.services.jobs import JobService
from utils import idempotency as _idem

logger = logging.getLogger("medanon")

_service = JobService()

# Rate limits — overridable via env. Defaults are intentionally conservative
# because each submission can spawn a long-running, resource-heavy job.
_RATE_JOBS_SUBMIT = os.environ.get("MEDANON_RATE_JOBS_SUBMIT", "30/minute")


async def _resolve_optional_target_url(user_url: str | None) -> str | None:
    """Validate and return a caller-supplied target URL, or None.

    Unlike the old behaviour, this helper does NOT fall back to the
    ``FHIR_TARGET_URL`` environment variable.  Export jobs (bulk-export,
    cohort, patient-export) should only upload to a target when the caller
    explicitly requests it — silent auto-injection caused unwanted uploads.

    Use :func:`_resolve_import_target_url` for bulk-import jobs, which do
    require a target and support the env-var fallback.
    """
    if user_url:
        await _validate_server_url(user_url)
        return user_url.rstrip("/")
    return None


async def _resolve_import_target_url(user_url: str | None) -> str | None:
    """Return a validated target URL for bulk-import jobs.

    Validates the caller-supplied URL when provided; falls back to the
    ``FHIR_TARGET_URL`` environment variable when the caller omits it.
    Returns ``None`` when neither is available (the endpoint will 400).
    """
    if user_url:
        await _validate_server_url(user_url)
        return user_url.rstrip("/")
    env_val = os.environ.get("FHIR_TARGET_URL", "").rstrip("/")
    return env_val or None


def _check_idempotency(request: Request, scope: str, body_model: BaseModel):
    """Return (idem_key, body_hash, cached_response_or_None).

    Job-submission endpoints are obvious idempotency targets: a network blip
    after the server enqueued the job would otherwise produce a duplicate
    bulk-export run.  The cached response (the original ``job_dict``) is
    returned unchanged so the client recovers the same ``job_id``.
    """
    try:
        idem_key = _idem.validate_key(request.headers.get("Idempotency-Key"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not idem_key:
        return None, "", None
    body_hash = _idem.hash_body(body_model.model_dump(mode="json", exclude_none=True))
    try:
        cached = _idem.lookup_or_conflict(scope, idem_key, body_hash)
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key reused with a different request body",
        )
    return idem_key, body_hash, cached
