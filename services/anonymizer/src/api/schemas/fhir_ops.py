"""Pydantic request models for FHIR server integration endpoints.

Replaces the manual ``req_data = await _parse_json_body(request)`` +
``req_data.get(...)`` pattern in api/routers/fhir_server.py.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class FromServerRequest(BaseModel):
    """Request body for POST /process/from-server."""

    server_url: str | None = None
    resource_types: list[str] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    token: str | None = None
    timeout: float = 30.0


class EverythingRequest(BaseModel):
    """Request body for POST /process/everything."""

    server_url: str | None = None
    resource_type: str
    resource_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    token: str | None = None
    timeout: float = 30.0


class AndUploadRequest(BaseModel):
    """Request body for POST /process/and-upload."""

    target_server_url: str | None = None
    resource: dict[str, Any]
    target_token: str | None = None
    timeout: float = 30.0


class RoundTripRequest(BaseModel):
    """Request body for POST /process/round-trip."""

    source_server_url: str | None = None
    target_server_url: str | None = None
    resource_types: list[str] | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    source_token: str | None = None
    target_token: str | None = None
    timeout: float = 30.0


class BulkExportRequest(BaseModel):
    """Request body for POST /process/bulk-export."""

    server_url: str | None = None
    level: Literal["system", "type"] = "system"
    resource_type: str | None = None
    type_filter: str | None = None
    since: str | None = None
    token: str | None = None
    timeout: float = 30.0


class CohortRequest(BaseModel):
    """Request body for POST /process/cohort."""

    server_url: str | None = None
    search_type: str
    search_params: dict[str, Any] = Field(default_factory=dict)
    everything_params: dict[str, Any] = Field(default_factory=dict)
    token: str | None = None
    timeout: float = 30.0
