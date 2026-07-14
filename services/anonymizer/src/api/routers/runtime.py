"""Open runtime-config endpoint.

``GET /v1/runtime-config`` is an open path (no auth) that returns the small,
**non-secret** slice of deployment configuration the SPA needs before/without
login to route FHIR reads: which saved server is the active source/target and
whether the bundled (built-in) FHIR servers exist in this deployment.

Admin-managed instance settings live behind ``/v1/settings`` (admin-only); this
endpoint deliberately exposes only ``active_source_id`` / ``active_target_id``
(opaque ids  no URLs, no tokens) plus ``builtin_fhir_enabled`` so every user's
routing layer can resolve the right server. Browsing a saved server still goes
through the SSRF-guarded ``/v1/fhir-proxy?source_id=`` where the token is applied
server-side.
"""

from __future__ import annotations

import os

from fastapi import APIRouter

router = APIRouter(prefix="/v1", tags=["runtime"])


def _builtin_fhir_enabled() -> bool:
    """Whether the bundled source/target HAPI servers exist in this deployment.

    Defaults to True (the bundled docker stack). Set MEDANON_BUILTIN_FHIR=false in
    a client deployment where only the client's own saved servers are reachable.
    """
    return os.environ.get("MEDANON_BUILTIN_FHIR", "true").strip().lower() != "false"


@router.get("/runtime-config")
def get_runtime_config() -> dict:
    """Return the non-secret routing config for the SPA (open path)."""
    from api.services.settings import SettingsService

    settings = SettingsService().get()
    return {
        "active_source_id": settings.get("active_source_id", "source"),
        "active_target_id": settings.get("active_target_id", "target"),
        "builtin_fhir_enabled": _builtin_fhir_enabled(),
    }
