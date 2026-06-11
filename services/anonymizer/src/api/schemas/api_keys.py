"""Pydantic models for per-client API key management."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_CLIENT_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")
_VALID_ROLES = {"admin", "analyst", "viewer"}


class ApiKeyCreateRequest(BaseModel):
    client_id: str = Field(
        description="Human-readable client identifier (1-64 alphanumeric chars)"
    )
    role: str = Field(
        default="analyst", description="RBAC role: admin | analyst | viewer"
    )
    expires_at: str | None = Field(
        default=None, description="ISO-8601 expiry timestamp (null = never)"
    )
    description: str = Field(
        default="", max_length=256, description="Optional free-text description"
    )

    @field_validator("client_id")
    @classmethod
    def _validate_client_id(cls, v: str) -> str:
        if not _CLIENT_ID_RE.match(v):
            raise ValueError(
                "client_id must be 1-64 alphanumeric/dash/underscore characters"
            )
        return v

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in _VALID_ROLES:
            raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}")
        return v


class ApiKeyResponse(BaseModel):
    """Metadata returned for all key operations (never includes raw key or hash)."""

    id: str
    client_id: str
    role: str
    created_at: str
    expires_at: str | None
    revoked: bool
    last_used_at: str | None
    description: str


class ApiKeyCreateResponse(ApiKeyResponse):
    """Response on key creation — includes raw key (returned exactly once)."""

    raw_key: str = Field(
        description="Secret API key — store securely; not retrievable again"
    )
