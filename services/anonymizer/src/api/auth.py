"""Keycloak OIDC + legacy API-key dual-auth module.

When KEYCLOAK_URL is set:
  - Bearer JWTs are validated against Keycloak JWKS (RS256, cached).
  - RBAC is enforced per-endpoint via realm roles (admin > analyst > viewer).
  - A service-account token is available for backend-to-backend calls.

When only MEDANON_API_KEY is set:
  - X-API-Key header must match (legacy scripts / CI).

When neither is configured:
  - Open access (backward-compatible).
"""

import json
import logging
import logging.handlers
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from fastapi import HTTPException, Request

log = logging.getLogger("medanon.auth")

# ---------------------------------------------------------------------------
# Configuration from env
# ---------------------------------------------------------------------------
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "").strip()
KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "medanon")
KEYCLOAK_CLIENT_ID = os.environ.get("KEYCLOAK_CLIENT_ID", "medanon-api")
KEYCLOAK_CLIENT_SECRET = os.environ.get("KEYCLOAK_CLIENT_SECRET", "")
_API_KEY = os.environ.get("MEDANON_API_KEY", "").strip()

OPEN_PATHS = frozenset({
    "/health", "/ready", "/metrics", "/docs", "/openapi.json", "/redoc", "/",
})

ENDPOINT_ROLES: dict[str, str] = {
    "/process": "analyst",
    "/process/raw": "analyst",
    "/process/ndjson": "analyst",
    "/process/batch": "analyst",
    "/process/from-server": "analyst",
    "/process/everything": "analyst",
    "/analyse/risk": "analyst",
    "/generate/synthetic": "analyst",
    "/process/and-upload": "admin",
    "/process/round-trip": "admin",
}


# ---------------------------------------------------------------------------
# Role hierarchy
# ---------------------------------------------------------------------------
class Role(IntEnum):
    VIEWER = 0
    ANALYST = 1
    ADMIN = 2


_ROLE_MAP = {r.name.lower(): r for r in Role}


@dataclass(frozen=True)
class AuthContext:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)
    auth_method: str = "anonymous"
    token_claims: dict = field(default_factory=dict)

    def has_role(self, role_name: str) -> bool:
        """Return True if this context satisfies *role_name* (hierarchy-aware)."""
        required = _ROLE_MAP.get(role_name.lower())
        if required is None:
            return False
        best = max((_ROLE_MAP[r] for r in self.roles if r in _ROLE_MAP), default=Role.VIEWER)
        return best >= required


# ---------------------------------------------------------------------------
# JWKS / JWT validation (lazy — only when KEYCLOAK_URL is set)
# ---------------------------------------------------------------------------
_jwk_client = None


def _get_jwk_client():
    global _jwk_client
    if _jwk_client is None:
        import jwt  # PyJWT
        issuer_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}"
        jwks_url = f"{issuer_url}/protocol/openid-connect/certs"
        _jwk_client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300)
    return _jwk_client


def validate_jwt(token: str) -> AuthContext:
    """Decode and validate a Keycloak RS256 JWT; returns an AuthContext."""
    import jwt as pyjwt

    client = _get_jwk_client()
    signing_key = client.get_signing_key_from_jwt(token)
    issuer = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}"
    claims = pyjwt.decode(
        token,
        signing_key.key,
        algorithms=["RS256"],
        audience="account",
        issuer=issuer,
        options={"verify_exp": True},
    )
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    return AuthContext(
        subject=claims.get("preferred_username") or claims.get("sub", "unknown"),
        roles=frozenset(r.lower() for r in realm_roles),
        auth_method="jwt",
        token_claims=claims,
    )


# ---------------------------------------------------------------------------
# Unified auth context resolver
# ---------------------------------------------------------------------------
def get_auth_context(request: Request) -> AuthContext:
    """Resolve auth from the request; raises HTTPException(401) on failure."""
    auth_header = request.headers.get("Authorization", "")

    # 1. Try JWT if Keycloak is configured
    if KEYCLOAK_URL and auth_header.lower().startswith("bearer "):
        token = auth_header.split(None, 1)[1]
        try:
            return validate_jwt(token)
        except Exception as exc:
            log.warning("JWT validation failed: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    # 2. Try legacy API key
    api_key_header = request.headers.get("X-API-Key", "")
    if _API_KEY:
        if api_key_header == _API_KEY:
            return AuthContext(subject="api-key-user", roles=frozenset({"admin"}), auth_method="api-key")
        if not KEYCLOAK_URL:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # 3. If Keycloak is required but no credentials provided
    if KEYCLOAK_URL:
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 4. No auth configured at all — open access (no API key, no Keycloak)
    if not _API_KEY:
        return AuthContext(subject="anonymous", roles=frozenset({"admin"}), auth_method="none")

    raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------------------------------------------------------------------
# Service-account token (client-credentials grant) for backend calls
# ---------------------------------------------------------------------------
_svc_token: Optional[str] = None
_svc_token_exp: float = 0.0
_svc_lock = threading.Lock()


def get_service_token() -> str:
    """Get a cached service-account access token from Keycloak (client_credentials)."""
    global _svc_token, _svc_token_exp
    if _svc_token and time.time() < _svc_token_exp:
        return _svc_token

    with _svc_lock:
        if _svc_token and time.time() < _svc_token_exp:
            return _svc_token
        import httpx
        token_url = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
        resp = httpx.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": KEYCLOAK_CLIENT_ID,
            "client_secret": KEYCLOAK_CLIENT_SECRET,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        _svc_token = data["access_token"]
        _svc_token_exp = time.time() + data.get("expires_in", 300) - 30
        return _svc_token


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------
_AUDIT_LOG_FILE = os.environ.get("MEDANON_AUDIT_LOG_FILE", "")
_audit_logger: Optional[logging.Logger] = None


def _get_audit_logger() -> Optional[logging.Logger]:
    global _audit_logger
    if _audit_logger is not None:
        return _audit_logger
    if not _AUDIT_LOG_FILE:
        return None
    _audit_logger = logging.getLogger("medanon.audit")
    _audit_logger.setLevel(logging.INFO)
    _audit_logger.propagate = False
    # Rotating file handler: 10 MB per file, keep 5 backups (50 MB total)
    max_bytes = int(os.environ.get("MEDANON_AUDIT_LOG_MAX_BYTES", 10 * 1024 * 1024))
    backup_count = int(os.environ.get("MEDANON_AUDIT_LOG_BACKUP_COUNT", 5))
    handler = logging.handlers.RotatingFileHandler(
        _AUDIT_LOG_FILE,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_logger.addHandler(handler)
    return _audit_logger


def log_audit(request: Request, status_code: int, auth: Optional[AuthContext] = None):
    """Write a structured JSON audit entry (never logs PHI or raw tokens)."""
    al = _get_audit_logger()
    if al is None:
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": request.method,
        "path": request.url.path,
        "status": status_code,
        "request_id": request.headers.get("X-Request-ID", "-"),
        "subject": auth.subject if auth else "anonymous",
        "auth_method": auth.auth_method if auth else "none",
    }
    al.info(json.dumps(entry, separators=(",", ":")))
