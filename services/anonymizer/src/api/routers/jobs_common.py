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

# Rate limits  overridable via env. Defaults are intentionally conservative
# because each submission can spawn a long-running, resource-heavy job.
_RATE_JOBS_SUBMIT = os.environ.get("MEDANON_RATE_JOBS_SUBMIT", "30/minute")


async def _resolve_optional_target_url(user_url: str | None) -> str | None:
    """Validate and return a caller-supplied target URL, or None.

    Unlike the old behaviour, this helper does NOT fall back to the
    ``FHIR_TARGET_URL`` environment variable.  Export jobs (bulk-export,
    cohort, patient-export) should only upload to a target when the caller
    explicitly requests it  silent auto-injection caused unwanted uploads.

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


async def _resolve_source_url(source_id: str, fallback_url: str) -> str:
    """Return the validated server URL for a saved input source.

    Loads the source's ``server_url`` from the connector store and SSRF-validates
    it (a saved URL is still passed through the same guard as a caller-supplied
    one). Falls back to *fallback_url* when the store or source is unavailable so
    an explicit ``server_url`` on the request still works.
    """
    try:
        from integrations.connectors import get_source_store

        store = get_source_store()
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="Input-source store not initialised (set MEDANON_APP_DB_URL).",
            )
        meta = store.get(source_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("source_resolve_failed id=%s: %s", source_id, exc)
        raise HTTPException(status_code=500, detail="Input source error") from exc
    if meta is None:
        raise HTTPException(
            status_code=404, detail=f"Input source not found: {source_id}"
        )
    url = meta.get("server_url") or fallback_url
    await _validate_server_url(url)
    return url.rstrip("/")


def _saved_id_from_active(active: str | None) -> str | None:
    """Map a deployment active id to a saved-server id, or None.

    The instance-settings ``active_source_id``/``active_target_id`` may be a
    built-in (``source``/``target``), a browser-local custom id (not resolvable
    server-side), or a saved server (optionally ``saved:`` prefixed). Only a saved
    server that actually exists in the store is returned  otherwise the caller
    falls back to ``FHIR_SOURCE_URL``/``FHIR_TARGET_URL``.
    """
    active = (active or "").strip()
    if active.startswith("saved:"):
        active = active[len("saved:") :]
    if not active or active in ("source", "target"):
        return None
    from integrations.connectors import get_source_store

    store = get_source_store()
    if store is None:
        return None
    try:
        return active if store.get(active) else None
    except Exception:
        return None


def _deployment_source_id() -> str | None:
    """Saved source-server id from the deployment active source (or None)."""
    try:
        from api.services.settings import SettingsService

        return _saved_id_from_active(SettingsService().get().get("active_source_id"))
    except Exception:
        return None


def _deployment_target_id() -> str | None:
    """Saved target-server id from the deployment active target (or None)."""
    try:
        from api.services.settings import SettingsService

        return _saved_id_from_active(SettingsService().get().get("active_target_id"))
    except Exception:
        return None


def effective_source_id(
    req_source_id: str | None, req_server_url: str | None
) -> str | None:
    """Source id to use: explicit wins; else deployment default when no URL given."""
    if req_source_id:
        return req_source_id
    if req_server_url:
        return None  # explicit URL  don't override with a saved server
    return _deployment_source_id()


def effective_target_id(
    req_target_id: str | None, req_target_url: str | None
) -> str | None:
    """Target id to use: explicit wins; else deployment default when no URL given."""
    if req_target_id:
        return req_target_id
    if req_target_url:
        return None
    return _deployment_target_id()


async def _resolve_target_url(target_id: str, fallback_url: str | None) -> str | None:
    """Return the validated URL for a saved target FHIR server (mirrors source)."""
    try:
        from integrations.connectors import get_source_store

        store = get_source_store()
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="FHIR-server store not initialised (set MEDANON_APP_DB_URL).",
            )
        meta = store.get(target_id)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("target_resolve_failed id=%s: %s", target_id, exc)
        raise HTTPException(status_code=500, detail="Target server error") from exc
    if meta is None:
        raise HTTPException(
            status_code=404, detail=f"Target FHIR server not found: {target_id}"
        )
    url = meta.get("server_url") or fallback_url
    if url:
        await _validate_server_url(url)
        return url.rstrip("/")
    return None


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
