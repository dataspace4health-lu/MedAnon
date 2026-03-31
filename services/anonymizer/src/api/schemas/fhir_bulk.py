"""FHIR Bulk Data Access IG response schemas."""
from __future__ import annotations

from pydantic import BaseModel


class BulkExportOutputFile(BaseModel):
    type: str   # FHIR resource type, e.g. "Patient"
    url: str    # Download URL for this NDJSON file


class BulkExportManifest(BaseModel):
    """Response body for a completed bulk export poll (HTTP 200)."""

    transactionTime: str
    request: str
    requiresAccessToken: bool = False
    output: list[BulkExportOutputFile]
    error: list[dict] = []
    deleted: list[dict] = []


class BulkExportPendingResponse(BaseModel):
    """Included in X-Progress header while export is in progress."""

    status: str
    job_id: str
