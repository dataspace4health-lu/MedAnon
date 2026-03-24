"""API-key authentication module.

When MEDANON_API_KEY is set:
  - X-API-Key header must match on all non-open endpoints.
  - RBAC is enforced per-endpoint via roles (admin > analyst > viewer).

When MEDANON_API_KEY is not set:
  - Open access (all users get admin role).
"""

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
    "/process/bulk-export": "admin",
    "/process/cohort": "analyst",
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

    def has_role(self, role_name: str) -> bool:
        """Return True if this context satisfies *role_name* (hierarchy-aware)."""
        required = _ROLE_MAP.get(role_name.lower())
        if required is None:
            return False
        best = max((_ROLE_MAP[r] for r in self.roles if r in _ROLE_MAP), default=Role.VIEWER)
        return best >= required


# ---------------------------------------------------------------------------
# Unified auth context resolver
# ---------------------------------------------------------------------------
def get_auth_context(request: Request) -> AuthContext:
    """Resolve auth from the request; raises HTTPException(401) on failure."""
    # 1. Try API key
    api_key_header = request.headers.get("X-API-Key", "")
    if _API_KEY:
        if api_key_header == _API_KEY:
            return AuthContext(subject="api-key-user", roles=frozenset({"admin"}), auth_method="api-key")
        raise HTTPException(status_code=401, detail="Unauthorized")

    # 2. No auth configured — open access
    return AuthContext(subject="anonymous", roles=frozenset({"admin"}), auth_method="none")


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
