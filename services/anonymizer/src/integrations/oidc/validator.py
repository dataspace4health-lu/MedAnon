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


def _jwks_url() -> str:
    url = os.environ.get("OIDC_JWKS_URL", "").strip()
    if not url and _issuer():
        # OIDC discovery: append /.well-known/jwks.json (works for Keycloak + Azure)
        iss = _issuer().rstrip("/")
        url = f"{iss}/.well-known/jwks.json"
        if "microsoftonline.com" in iss:
            # Azure uses openid-configuration → keys_endpoint; JWKS is at /discovery/keys
            url = f"{iss}/discovery/keys"
    return url


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


def extract_roles(claims: dict) -> frozenset[str]:
    """Map JWT claim roles → internal role names using OIDC_ROLE_MAP."""
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
    # Default to viewer when authenticated but no recognized role
    return internal if internal else frozenset({"viewer"})


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
