"""Trust Gate audit-profile management endpoints.

CRUD for the named phase/threshold/target selections that tune the pre-privacy
Trust Gate barrier:

    GET    /v1/trust-profiles             list all (system + user-defined)
    GET    /v1/trust-profiles/{name}      fetch one
    POST   /v1/trust-profiles             create a user-defined profile
    PUT    /v1/trust-profiles/{name}      update a user-defined profile
    DELETE /v1/trust-profiles/{name}      delete a user-defined profile

System profiles (the bundled starters) are read-only  PUT/DELETE return 403.
All write operations require the 'admin' role. The available phase ids are listed
at ``GET /v1/trust-profiles/_phases``.
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request

from api.auth import AuthContext
from api.schemas.trust_profiles import TrustProfileCreate, TrustProfileUpdate
from pipeline.trust_profile import PHASE_IDS, get_trust_profile_store

router = APIRouter()
logger = logging.getLogger("medanon")

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _get_store():
    store = get_trust_profile_store()
    if store is None:
        raise HTTPException(
            status_code=503, detail="Trust profile store not initialised."
        )
    return store


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                "Trust profile name must be 1–64 characters and contain only "
                "letters, digits, hyphens, and underscores."
            ),
        )


def _require_admin(request: Request) -> None:
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(
            status_code=403, detail="Admin role required for trust-profile writes."
        )


@router.get("/trust-profiles/_phases")
def list_phases(request: Request):
    """Return the catalog of selectable audit phases."""
    return {"phases": list(PHASE_IDS)}


@router.get("/trust-profiles")
def list_trust_profiles(request: Request):
    """List all trust profiles  system (read-only) and user-defined."""
    return {"profiles": _get_store().list_all()}


@router.get("/trust-profiles/{name}")
def get_trust_profile(name: str, request: Request):
    _validate_name(name)
    meta = _get_store().get(name)
    if meta is None:
        raise HTTPException(
            status_code=404, detail=f"Trust profile '{name}' not found."
        )
    return {"profile": meta}


@router.post("/trust-profiles", status_code=201)
def create_trust_profile(body: TrustProfileCreate, request: Request):
    _require_admin(request)
    _validate_name(body.name)
    store = _get_store()
    if store.exists(body.name):
        raise HTTPException(
            status_code=409,
            detail=f"Trust profile '{body.name}' already exists. Use PUT to update.",
        )
    profile = store.create(
        body.name,
        body.description,
        body.phases,
        body.thresholds,
        body.targets,
        body.intended_use,
        body.use_case,
    )
    logger.info("trust_profile_created name=%s", body.name)
    return {"profile": profile}


@router.put("/trust-profiles/{name}")
def update_trust_profile(name: str, body: TrustProfileUpdate, request: Request):
    _require_admin(request)
    _validate_name(name)
    store = _get_store()
    try:
        profile = store.update(
            name,
            description=body.description,
            phases=body.phases,
            thresholds=body.thresholds,
            targets=body.targets,
            intended_use=body.intended_use,
            use_case=body.use_case,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    logger.info("trust_profile_updated name=%s", name)
    return {"profile": profile}


@router.delete("/trust-profiles/{name}", status_code=204)
def delete_trust_profile(name: str, request: Request):
    _require_admin(request)
    _validate_name(name)
    store = _get_store()
    try:
        store.delete(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    logger.info("trust_profile_deleted name=%s", name)
