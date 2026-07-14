"""Auth configuration endpoint.

``GET /v1/auth/config`` is an open path (no API key required) that returns the
runtime authentication configuration so the React SPA can dynamically configure
its OIDC client without a UI rebuild.

Switching Keycloak → Azure AD is an env-only change:
  - restart the anonymizer
  - React fetches /v1/auth/config
  - oidc-client-ts reconfigures against the new authority

"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.get("/config")
def get_auth_config() -> dict:
    """Return the current authentication provider configuration.

    This endpoint is intentionally open (listed in OPEN_PATHS) so the SPA can
    fetch it before the user is authenticated.  No secrets are exposed  only
    the provider name, OIDC authority, and client_id.
    """
    from api.auth_providers import auth_config

    return auth_config()
