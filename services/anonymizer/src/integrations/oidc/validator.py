"""Generic OIDC JWT validator — provider-agnostic (Keycloak, Azure AD, Auth0 ...).

Configured entirely by env vars; swapping Keycloak for Azure AD is a
config-only change with zero code changes:

    # Keycloak
    OIDC_ISSUER=https://keycloak.example.com/realms/medanon
    OIDC_ROLE_CLAIM_PATH=realm_access.roles      # dotted path into JWT claims
    OIDC_ROLE_MAP={"medanon-admin":"admin","medanon-analyst":"analyst","medanon-viewer":"viewer"}

    # Azure AD / Entra
    OIDC_ISSUER=https://login.microsoftonline.com/<tenant>/v2.0
    OIDC_ROLE_CLAIM_PATH=roles
    OIDC_ROLE_MAP={"MedAnon.Admin":"admin","MedAnon.Analyst":"analyst","MedAnon.Viewer":"viewer"}

The module imports ``jwt`` (PyJWT) and ``jwt.algorithms`` lazily so that the
module can be imported in environments where PyJWT is not installed — functions
return ``None`` or raise ``ImportError`` in that case.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("medanon.oidc")

# ---------------------------------------------------------------------------
# Env-var config (read at call time so tests can patch via monkeypatch.setenv)
# ---------------------------------------------------------------------------


def _issuer() -> str:
    return os.environ.get("OIDC_ISSUER", "").strip()


def _discovery_base() -> str:
    """Base URL the *backend* uses to reach the IdP for JWKS/discovery.

    In a split-horizon deployment (Keycloak behind a reverse proxy) the browser
    reaches Keycloak at the public ``OIDC_ISSUER`` URL, but the anonymizer
    container must reach it at an internal Docker hostname. Set
    ``OIDC_DISCOVERY_BASE`` to that internal realm URL, e.g.
    ``http://medanon-keycloak:8080/auth/realms/medanon``. Falls back to the
    public issuer when unset (single-host / backchannel-dynamic deployments).
    """
    return (os.environ.get("OIDC_DISCOVERY_BASE", "").strip() or _issuer()).rstrip("/")


def _discover_jwks_uri(base: str) -> str:
    """Fetch the OIDC discovery document and return its ``jwks_uri``.

    Provider-agnostic: works for Keycloak, Azure AD, Auth0, Okta. Returns an
    empty string on any failure so the caller can fall back to derivation.
    """
    import urllib.request

    well_known = f"{base}/.well-known/openid-configuration"
    try:
        with urllib.request.urlopen(well_known, timeout=5) as resp:  # noqa: S310
            doc = json.loads(resp.read().decode("utf-8"))
        return str(doc.get("jwks_uri", "")).strip()
    except Exception as exc:
        logger.warning("oidc_discovery_failed url=%s: %s", well_known, exc)
        return ""


def _derive_jwks_url(base: str) -> str:
    """Best-effort JWKS URL derivation by provider shape (discovery fallback)."""
    if "/realms/" in base:
        # Keycloak: realm keys live at /protocol/openid-connect/certs
        return f"{base}/protocol/openid-connect/certs"
    if "microsoftonline.com" in base:
        # Azure AD: JWKS is at /discovery/keys
        return f"{base}/discovery/keys"
    # Auth0 / Okta / generic OIDC
    return f"{base}/.well-known/jwks.json"


def _jwks_url() -> str:
    # 1. Explicit override — recommended for split-horizon / locked-down networks.
    url = os.environ.get("OIDC_JWKS_URL", "").strip()
    if url:
        return url
    if not _issuer():
        return ""
    base = _discovery_base()
    # 2. Standards-correct: read jwks_uri from the discovery document.
    discovered = _discover_jwks_uri(base)
    if discovered:
        return discovered
    # 3. Fallback: derive by provider shape when discovery is unreachable.
    return _derive_jwks_url(base)


def _audience() -> str | None:
    return os.environ.get("OIDC_AUDIENCE", "").strip() or None


def _role_claim_path() -> str:
    return os.environ.get("OIDC_ROLE_CLAIM_PATH", "realm_access.roles").strip()


def _role_map() -> dict[str, str]:
    raw = os.environ.get("OIDC_ROLE_MAP", "").strip()
    if not raw:
        # Default: Keycloak realm role names → internal roles
        return {
            "medanon-admin": "admin",
            "medanon-analyst": "analyst",
            "medanon-viewer": "viewer",
        }
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("OIDC_ROLE_MAP is not valid JSON — using defaults")
        return {}


def _username_claim() -> str:
    return os.environ.get("OIDC_USERNAME_CLAIM", "preferred_username").strip()


def _clock_skew_sec() -> int:
    return int(os.environ.get("OIDC_CLOCK_SKEW_SEC", "10"))


# ---------------------------------------------------------------------------
# JWKS client singleton (cached; re-instantiated on OIDC_ISSUER change)
# ---------------------------------------------------------------------------

_jwks_client = None
_jwks_client_url: str = ""


def _get_jwks_client():
    """Return a cached PyJWT PyJWKClient, creating one if OIDC_JWKS_URL is set."""
    global _jwks_client, _jwks_client_url

    url = _jwks_url()
    if not url:
        return None

    if _jwks_client is None or _jwks_client_url != url:
        try:
            import jwt as _jwt  # noqa: PLC0415

            _jwks_client = _jwt.PyJWKClient(url, cache_keys=True, lifespan=300)
            _jwks_client_url = url
            logger.info("oidc_jwks_client_init url=%s", url)
        except ImportError:
            logger.warning(
                "PyJWT not installed — OIDC validation disabled. "
                "Add PyJWT to requirements.txt to enable."
            )
            return None
        except Exception as exc:
            logger.warning("oidc_jwks_client_init_failed url=%s: %s", url, exc)
            return None

    return _jwks_client


def warmup() -> bool:
    """Eagerly fetch the JWKS and populate the key cache.  Non-fatal on failure."""
    client = _get_jwks_client()
    if client is None:
        return False
    try:
        # Fetch JWKS at startup so the first request is fast
        client.fetch_data()
        logger.info("oidc_jwks_warmup ok url=%s", _jwks_client_url)
        return True
    except Exception as exc:
        logger.warning("oidc_jwks_warmup_failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Claim path extractor
# ---------------------------------------------------------------------------


def _extract_claim(claims: dict, path: str) -> Any:
    """Walk a dotted claim path (e.g. ``realm_access.roles``) into *claims*."""
    cur: Any = claims
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# ---------------------------------------------------------------------------
# Role extraction
# ---------------------------------------------------------------------------


def _default_role() -> str:
    """Fallback role for an authenticated user with no mapped role.

    Empty (the default) = DENY BY DEFAULT: a user who authenticates but has no
    recognised role gets zero roles, and the OIDC provider rejects the request
    with 403. This is the production-safe posture for PHI — access must be
    explicitly granted, never implied by mere authentication. Set
    ``OIDC_DEFAULT_ROLE=viewer`` to restore the old "any authenticated user can
    read" behaviour.
    """
    return os.environ.get("OIDC_DEFAULT_ROLE", "").strip().lower()


def extract_roles(claims: dict) -> frozenset[str]:
    """Map JWT claim roles → internal role names using OIDC_ROLE_MAP.

    Returns an EMPTY set when the user has no mapped role and OIDC_DEFAULT_ROLE
    is unset (deny-by-default). Callers must treat an empty set as "no access".
    """
    role_values = _extract_claim(claims, _role_claim_path())
    if not isinstance(role_values, list):
        # scalar role (some providers emit a string)
        if isinstance(role_values, str):
            role_values = [role_values]
        else:
            role_values = []
    mapping = _role_map()
    internal = frozenset(
        mapping[r] for r in role_values if r in mapping
    )
    if internal:
        return internal
    default = _default_role()
    return frozenset({default}) if default else frozenset()


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------


def validate_jwt(token: str) -> dict | None:
    """Validate *token* and return its claims dict, or None if validation fails.

    Returns ``None`` (rather than raising) for soft failures like network
    errors fetching JWKS.  Raises ``HTTPException(401)`` for hard failures like
    invalid signature or expired token.
    """
    client = _get_jwks_client()
    if client is None:
        return None  # OIDC not configured

    try:
        import jwt as _jwt  # noqa: PLC0415
    except ImportError:
        return None

    issuer = _issuer()
    if not issuer:
        return None

    from fastapi import HTTPException

    try:
        signing_key = client.get_signing_key_from_jwt(token)
    except Exception as exc:
        logger.warning("oidc_jwks_fetch_failed: %s", exc)
        # Network failure fetching JWKS → treat as unavailable, not auth failure
        raise HTTPException(
            status_code=503, detail="OIDC provider temporarily unavailable"
        )

    try:
        options: dict = {
            "verify_exp": True,
            "verify_iss": True,
            "leeway": _clock_skew_sec(),
        }
        if _audience():
            options["verify_aud"] = True
        else:
            options["verify_aud"] = False

        claims = _jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
            issuer=issuer,
            audience=_audience(),
            options=options,
        )
        return claims
    except _jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except _jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}")


def oidc_enabled() -> bool:
    """True when OIDC_ISSUER is set and PyJWT is available."""
    if not _issuer():
        return False
    try:
        import jwt  # noqa: F401,PLC0415

        return True
    except ImportError:
        return False
