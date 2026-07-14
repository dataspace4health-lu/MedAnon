"""Instance-settings endpoints — deployment-wide admin-managed application defaults.

One shared settings row per deployment: which FHIR source/target the app is wired
to, the default de-identification rule profile, and the assessment defaults the UI
pre-fills. This is *instance* configuration, not per-user preferences, and it holds
no secrets (FHIR credentials live encrypted in the connector store).

  GET /v1/settings   read the effective settings (admin)
  PUT /v1/settings   merge a partial update into the settings row (admin)

Admin-only on both read and write: non-admin roles have no access to instance
configuration. Reads fall back to built-in defaults when no app DB is configured;
writes require the durable PostgreSQL store.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from api.auth import AuthContext
from api.deps import limiter
from api.services.settings import SettingsService, SettingsStoreUnavailable
from pipeline.config.service import is_valid_profile

router = APIRouter()
logger = logging.getLogger("medanon")

_settings = SettingsService()


def _require_admin(request: Request) -> AuthContext:
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(
            status_code=403, detail="Admin role required to manage settings."
        )
    return auth


class SettingsUpdate(BaseModel):
    """Partial update — only the provided fields change (PATCH semantics)."""

    config_profile: str | None = Field(default=None, min_length=1, max_length=128)
    fhir_page_size: int | None = Field(default=None, ge=50, le=1000)
    dataset_id: str | None = Field(default=None, max_length=256)
    source_system: str | None = Field(default=None, max_length=256)
    scan_max_resources: int | None = Field(default=None, ge=100, le=10_000_000)
    active_source_id: str | None = Field(default=None, min_length=1, max_length=256)
    active_target_id: str | None = Field(default=None, min_length=1, max_length=256)


@router.get("/settings")
async def get_settings(request: Request):
    """Return the effective instance settings. Admin only."""
    _require_admin(request)
    return _settings.get()


@router.put("/settings")
@limiter.limit("30/minute")
async def update_settings(body: SettingsUpdate, request: Request):
    """Merge a partial update into the deployment-wide settings row. Admin only."""
    auth = _require_admin(request)
    fields = body.model_dump(exclude_unset=True)
    # A stored config_profile that no job could load is a silent foot-gun — the
    # setting looks applied but every default-profile run would fail. Reject an
    # unknown profile here (built-in alias or an existing user config only).
    profile = fields.get("config_profile")
    if profile is not None and not is_valid_profile(profile):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown config_profile '{profile}'. Use a built-in alias "
                "(auto, minimal, gpas, gdpr, hipaa, research, structural, "
                "value-masking) or a user-defined config created via POST /v1/configs."
            ),
        )
    try:
        return _settings.update(fields, updated_by=auth.subject)
    except SettingsStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("settings_update_error: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Settings error") from exc
