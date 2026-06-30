"""Pluggable auth provider chain.

Controlled by ``MEDANON_AUTH_PROVIDER`` (default: ``auto``):

  ``auto``    — current legacy behaviour: DB key → env key → SMART bearer → open
  ``apikey``  — only X-API-Key accepted (no bearer, no open access)
  ``oidc``    — OIDC JWT (Bearer header) + optional X-API-Key dual-accept
                (``MEDANON_AUTH_ALLOW_API_KEY=true``, default true)
  ``none``    — open access (all callers granted admin; local dev only)

The existing ``get_auth_context()`` in ``api/auth.py`` delegates here when the
provider is not ``auto``, so the old code path is 100% preserved for legacy
deployments.

Provider selection can be changed at any time by editing ``MEDANON_AUTH_PROVIDER``
and restarting — no code changes needed.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from fastapi import HTTPException, Request

from api.auth import AuthContext

logger = logging.getLogger("medanon.auth.providers")

# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuthProvider(Protocol):
    """Minimal auth surface: inspect request → AuthContext or raise 401."""

    def authenticate(self, request: Request) -> AuthContext: ...

    @property
    def provider_name(self) -> str: ...


# ---------------------------------------------------------------------------
# Open provider (MEDANON_AUTH_PROVIDER=none)
# ---------------------------------------------------------------------------


class OpenProvider:
    """No authentication — every caller gets admin.  Local dev only."""

    provider_name = "none"

    def authenticate(self, request: Request) -> AuthContext:
        return AuthContext(subject="anonymous", roles=frozenset({"admin"}), auth_method="none")


# ---------------------------------------------------------------------------
# API-key provider (MEDANON_AUTH_PROVIDER=apikey)
# ---------------------------------------------------------------------------


class ApiKeyProvider:
    """Pure X-API-Key authentication.  Replicates the legacy DB+env logic."""

    provider_name = "apikey"

    def authenticate(self, request: Request) -> AuthContext:
        import hashlib
        import hmac

        api_key_header = request.headers.get("X-API-Key", "")
        env_key = os.environ.get("MEDANON_API_KEY", "").strip()

        # Import the DB key store singleton from auth module
        from api import auth as _auth

        if _auth._api_key_store is not None and api_key_header:
            key_hash = hashlib.sha256(api_key_header.encode()).hexdigest()
            row = _auth._api_key_store.lookup_by_hash(key_hash)
            if row:
                try:
                    _auth._api_key_store.touch_last_used(row["id"])
                except Exception:
                    pass
                return AuthContext(
                    subject=row["client_id"],
                    roles=frozenset({row["role"]}),
                    auth_method="api-key-db",
                )

        if env_key and api_key_header and hmac.compare_digest(api_key_header, env_key):
            return AuthContext(
                subject="api-key-user",
                roles=frozenset({"admin"}),
                auth_method="api-key",
            )

        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# OIDC provider (MEDANON_AUTH_PROVIDER=oidc)
# ---------------------------------------------------------------------------


class OidcProvider:
    """OIDC JWT via Bearer header, with optional API-key dual-accept.

    ``MEDANON_AUTH_ALLOW_API_KEY=true`` (default): X-API-Key still works so
    CLI workflows and service-to-service calls keep functioning while the
    organisation transitions to OIDC.

    Downgrade guard: if the Bearer token's ``iss`` matches ``OIDC_ISSUER`` but
    JWT validation fails, we reject with 401 rather than falling through to
    API-key — a compromised or expired token must not silently downgrade.
    """

    provider_name = "oidc"

    def _allow_api_key(self) -> bool:
        return os.environ.get("MEDANON_AUTH_ALLOW_API_KEY", "true").lower() in (
            "true", "1", "yes"
        )

    def _extract_bearer(self, request: Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        return None

    def _bearer_matches_oidc_issuer(self, token: str) -> bool:
        """Return True if the token's unverified ``iss`` claim matches OIDC_ISSUER."""
        try:
            import jwt as _jwt  # noqa: PLC0415

            # decode_complete without verification to peek at iss
            unverified = _jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            )
            issuer = os.environ.get("OIDC_ISSUER", "").strip()
            return bool(issuer and unverified.get("iss") == issuer)
        except Exception:
            return False

    def authenticate(self, request: Request) -> AuthContext:
        from integrations.oidc import validator as _oidc

        bearer = self._extract_bearer(request)
        api_key_header = request.headers.get("X-API-Key", "")

        # --- OIDC JWT path ---
        if bearer:
            # Downgrade guard: if iss matches our OIDC_ISSUER, validate strictly.
            if self._bearer_matches_oidc_issuer(bearer):
                claims = _oidc.validate_jwt(bearer)  # raises 401/503 on failure
                if claims is None:
                    raise HTTPException(
                        status_code=503, detail="OIDC provider not configured"
                    )
                roles = _oidc.extract_roles(claims)
                username_claim = os.environ.get(
                    "OIDC_USERNAME_CLAIM", "preferred_username"
                ).strip()
                subject = claims.get(username_claim) or claims.get("sub", "oidc-user")
                # Deny-by-default: a validly-authenticated user with no mapped
                # role gets zero roles → reject with 403 rather than silently
                # granting access. Assign a realm role (medanon-viewer/analyst/
                # admin) in Keycloak, or set OIDC_DEFAULT_ROLE to grant a floor.
                if not roles:
                    logger.warning(
                        "oidc_no_role subject=%s — rejecting (assign a realm role "
                        "or set OIDC_DEFAULT_ROLE)",
                        subject,
                    )
                    raise HTTPException(
                        status_code=403,
                        detail="Authenticated, but no authorized role is assigned to this account.",
                    )
                return AuthContext(
                    subject=str(subject),
                    roles=roles,
                    auth_method="oidc",
                )
            # Bearer from another issuer (SMART, legacy) — try SMART introspection
            try:
                from api.auth import _resolve_bearer_context

                return _resolve_bearer_context(bearer)
            except HTTPException:
                raise

        # --- API-key dual-accept ---
        if api_key_header and self._allow_api_key():
            try:
                return ApiKeyProvider().authenticate(request)
            except HTTPException:
                pass  # fall through to 401

        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDERS: dict[str, type] = {
    "none": OpenProvider,
    "apikey": ApiKeyProvider,
    "oidc": OidcProvider,
}

_active_provider: AuthProvider | None = None


def get_provider() -> AuthProvider | None:
    """Return the active provider, or None for ``auto`` mode (legacy path)."""
    global _active_provider
    mode = os.environ.get("MEDANON_AUTH_PROVIDER", "auto").strip().lower()
    if mode == "auto":
        return None

    # Re-construct if env changed (test-friendly)
    if _active_provider is None or _active_provider.provider_name != mode:
        cls = _PROVIDERS.get(mode)
        if cls is None:
            logger.warning(
                "Unknown MEDANON_AUTH_PROVIDER=%s — falling back to auto", mode
            )
            return None
        _active_provider = cls()
        logger.info("auth_provider=%s", mode)

    return _active_provider


def auth_config() -> dict:
    """Return the runtime auth configuration for the /v1/auth/config endpoint."""
    mode = os.environ.get("MEDANON_AUTH_PROVIDER", "auto").strip().lower()
    oidc_issuer = os.environ.get("OIDC_ISSUER", "").strip()
    config: dict = {
        "provider": mode,
        "oidc_enabled": bool(oidc_issuer),
    }
    if oidc_issuer:
        config["oidc_issuer"] = oidc_issuer
        config["oidc_client_id"] = os.environ.get("OIDC_CLIENT_ID", "medanon-ui").strip()
        config["oidc_scope"] = os.environ.get(
            "OIDC_SCOPE", "openid profile email"
        ).strip()
        # Whether API key dual-accept is on alongside OIDC
        config["api_key_accepted"] = os.environ.get(
            "MEDANON_AUTH_ALLOW_API_KEY", "true"
        ).lower() in ("true", "1", "yes")
    return config
