"""API-key authentication module.

When MEDANON_API_KEY is set:
  - X-API-Key header must match on all non-open endpoints.
  - RBAC is enforced per-endpoint via roles (admin > analyst > viewer).

When MEDANON_API_KEY is not set:
  - Open access (all users get admin role).
"""

import hmac
import json
import logging
import logging.handlers
import os
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

from fastapi import HTTPException, Request

log = logging.getLogger("medanon.auth")

# ---------------------------------------------------------------------------
# Configuration from env
# ---------------------------------------------------------------------------
_API_KEY = os.environ.get("MEDANON_API_KEY", "").strip()

if not _API_KEY:
    log.warning(
        "MEDANON_API_KEY is not set — running in open mode. "
        "All callers are granted admin privileges. "
        "This is only safe for local development. Set MEDANON_API_KEY in production."
    )

OPEN_PATHS = frozenset(
    {
        "/health",
        "/ready",
        "/metrics",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/",
        "/.well-known/smart-configuration",
    }
)

ENDPOINT_ROLES: dict[str, str] = {
    "/v1/process": "analyst",
    "/v1/process/raw": "analyst",
    "/v1/process/ndjson": "analyst",
    "/v1/process/batch": "analyst",
    "/v1/process/from-server": "analyst",
    "/v1/process/everything": "analyst",
    "/v1/process/dicom": "analyst",
    "/v1/process/dicom/batch": "analyst",
    "/v1/process/hl7v2": "analyst",
    "/v1/process/hl7v2/batch": "analyst",
    "/v1/analyse/risk": "analyst",
    "/v1/generate/synthetic": "analyst",
    "/v1/process/and-upload": "admin",
    "/v1/process/round-trip": "admin",
    "/v1/process/bulk-export": "admin",
    "/v1/process/cohort": "analyst",
    "/v1/jobs": "analyst",
    # Config profile management — list/read open to viewer; writes require admin
    # NOTE: POST /v1/configs requires admin — enforced via prefix match below.
    "/v1/configs": "viewer",
    # FHIR Bulk Data Access IG
    "/fhir/$export": "admin",
    "/fhir/Patient/$export": "analyst",
    # FHIR Subscriptions — list requires admin; CRUD handled by prefix below
    "/fhir/Subscription": "admin",
    # SMART token introspection
    "/oauth2/introspect": "analyst",
    # AI agent endpoints
    "/v1/ai/status": "viewer",
    "/v1/ai/generate-config": "admin",
    "/v1/ai/detect-pii": "analyst",
    "/v1/ai/explain": "analyst",
    "/v1/ai/compliance": "analyst",
    # Processing run history
    "/v1/processing-runs": "analyst",
    "/v1/processing-runs/stats": "analyst",
}

# Prefix-based role mapping for parameterized paths (e.g. /v1/jobs/{job_id}).
# Checked when ENDPOINT_ROLES produces no exact match.
ENDPOINT_ROLE_PREFIXES: dict[str, str] = {
    "/v1/admin/": "admin",
    "/v1/jobs/bulk-export": "admin",  # exact — listed first for priority
    "/v1/jobs/bulk-import": "admin",  # uploads to target FHIR server — requires admin
    "/v1/jobs/batch-patient-export": "analyst",
    "/v1/jobs/cohort": "analyst",
    "/v1/jobs/": "analyst",  # covers /v1/jobs/{id} and /v1/jobs/{id}/result
    "/v1/configs/": "viewer",  # covers /v1/configs/{name} — writes enforce admin in router
    "/fhir/Group/": "admin",  # /fhir/Group/{id}/$export
    "/fhir/export-status/": "analyst",  # /fhir/export-status/{job_id}
    "/fhir/Subscription/": "analyst",  # /fhir/Subscription/{id} CRUD
    "/v1/ai/": "analyst",  # AI agent endpoints (generate-config enforced as admin in ENDPOINT_ROLES)
    "/v1/processing-runs/": "analyst",  # covers /v1/processing-runs/{id}
}


def get_required_role(path: str) -> str | None:
    """Return the minimum required role for *path*, or None if unrestricted."""
    role = ENDPOINT_ROLES.get(path)
    if role is None:
        for prefix, r in ENDPOINT_ROLE_PREFIXES.items():
            if path.startswith(prefix):
                role = r
                break
    return role


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

    def has_role(self, role_name: str) -> bool:
        """Return True if this context satisfies *role_name* (hierarchy-aware)."""
        required = _ROLE_MAP.get(role_name.lower())
        if required is None:
            return False
        best = max(
            (_ROLE_MAP[r] for r in self.roles if r in _ROLE_MAP), default=Role.VIEWER
        )
        return best >= required


# ---------------------------------------------------------------------------
# Unified auth context resolver
# ---------------------------------------------------------------------------
def get_auth_context(request: Request) -> AuthContext:
    """Resolve auth from the request; raises HTTPException(401) on failure.

    Auth priority:
    1. X-API-Key header (API key mode)
    2. Authorization: Bearer <token> (SMART bearer token)
    3. Open access when MEDANON_API_KEY is not configured
    """
    api_key_header = request.headers.get("X-API-Key", "")
    bearer_token = _extract_bearer(request)

    if _API_KEY:
        # API-key auth
        if hmac.compare_digest(api_key_header, _API_KEY):
            return AuthContext(
                subject="api-key-user",
                roles=frozenset({"admin"}),
                auth_method="api-key",
            )
        # SMART bearer token auth
        if bearer_token:
            return _resolve_bearer_context(bearer_token)
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Open access — no auth configured
    return AuthContext(
        subject="anonymous", roles=frozenset({"admin"}), auth_method="none"
    )


def _extract_bearer(request: Request) -> str | None:
    """Return the bearer token from the Authorization header, or None."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return None


def _resolve_bearer_context(token: str) -> AuthContext:
    """Map a bearer token to an AuthContext using SMART introspection or API key fallback."""
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    introspection_url = os.environ.get("SMART_INTROSPECTION_URL", "").strip()

    if introspection_url:
        try:
            body = urllib.parse.urlencode({"token": token}).encode("utf-8")
            req = urllib.request.Request(
                introspection_url,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                data = json.loads(resp.read())
            if not data.get("active", False):
                raise HTTPException(status_code=401, detail="Token inactive")
            scope = data.get("scope", "")
            from api.smart_scopes import parse_smart_scopes

            role = parse_smart_scopes(scope)
            sub = data.get("sub", data.get("username", "smart-user"))
            return AuthContext(
                subject=sub, roles=frozenset({role}), auth_method="smart-bearer"
            )
        except HTTPException:
            raise
        except urllib.error.URLError as exc:
            # Network-level failure (DNS, timeout, connection refused) — report
            # as a service unavailability, not a 401.
            log.warning(
                "smart_introspection_unreachable url=%s: %s", introspection_url, exc
            )
            raise HTTPException(
                status_code=503, detail="Token introspection service unavailable"
            )
        except (ValueError, KeyError) as exc:
            # Malformed response from introspection endpoint
            log.warning("smart_introspection_bad_response: %s", exc)
            raise HTTPException(
                status_code=503, detail="Token introspection service unavailable"
            )
        except Exception:
            raise HTTPException(status_code=401, detail="Unauthorized")

    # Fallback: accept the API key as a bearer token
    if _API_KEY and hmac.compare_digest(token, _API_KEY):
        return AuthContext(
            subject="api-key-user",
            roles=frozenset({"admin"}),
            auth_method="bearer-apikey",
        )

    raise HTTPException(status_code=401, detail="Unauthorized")


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
