"""Request schemas for the async job queue endpoints."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field, model_validator


class BulkExportJobRequest(BaseModel):
    """Request body for POST /v1/jobs/bulk-export."""

    server_url: str | None = None
    level: str = "system"
    resource_type: str | None = None
    type_filter: str | None = None
    since: str | None = None
    token: str | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    config_profile: str = "auto"
    target_url: str | None = Field(
        default=None,
        description="Target FHIR server URL. De-identified resources are PUT here after processing. Defaults to FHIR_TARGET_URL env var when set.",
    )
    target_token: str | None = None


class CohortJobRequest(BaseModel):
    """Request body for POST /v1/jobs/cohort."""

    server_url: str | None = None
    search_type: str
    search_params: dict[str, Any] = Field(default_factory=dict)
    everything_params: dict[str, Any] = Field(default_factory=dict)
    token: str | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    config_profile: str = "auto"
    target_url: str | None = Field(
        default=None,
        description="Target FHIR server URL. De-identified resources are PUT here after processing. Defaults to FHIR_TARGET_URL env var when set.",
    )
    target_token: str | None = None


class PatientExportJobRequest(BaseModel):
    """Request body for POST /v1/jobs/patient-export."""

    server_url: str | None = None
    patient_id: str
    patient_name: str | None = None
    token: str | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    config_profile: str = "auto"
    target_url: str | None = Field(
        default=None,
        description="Target FHIR server URL. De-identified resources are PUT here after processing. Defaults to FHIR_TARGET_URL env var when set.",
    )
    target_token: str | None = None


class BatchPatientExportRequest(BaseModel):
    """Request body for POST /v1/jobs/batch-patient-export."""

    server_url: str | None = None
    patient_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=500,
        description="List of Patient resource IDs to export and de-identify.",
    )
    patient_names: dict[str, str] | None = Field(
        default=None,
        description="Optional mapping of patient ID to display name (for job labels).",
    )
    token: str | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    config_profile: str = "auto"
    target_url: str | None = Field(
        default=None,
        description="Target FHIR server URL. De-identified resources are PUT here after processing.",
    )
    target_token: str | None = None


class BulkImportJobRequest(BaseModel):
    """Request body for POST /v1/jobs/bulk-import.

    Reads a completed NDJSON result (from a previous export job or an explicit
    path) and uploads de-identified resources to the target FHIR server using
    parallel tier-aware FHIR batch Bundles.

    Either ``job_id`` or ``ndjson_path`` must be supplied.
    """

    job_id: str | None = Field(
        default=None,
        description="ID of a completed bulk-export / cohort / patient-export job whose NDJSON result will be uploaded.",
    )
    ndjson_path: str | None = Field(
        default=None,
        description="Explicit NDJSON path (local file path or s3://bucket/key). Used when no source job_id is given.",
    )
    target_url: str | None = Field(
        default=None,
        description="Target FHIR server URL. Falls back to FHIR_TARGET_URL env var when not provided.",
    )
    target_token: str | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    parallel: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Number of FHIR batch Bundles to POST concurrently within each topological tier.",
    )
    batch_size: int = Field(
        default=500,
        ge=1,
        le=2000,
        description="Resources per FHIR batch Bundle. Overrides MEDANON_UPLOAD_BATCH_SIZE for this job.",
    )

    @model_validator(mode="after")
    def _check_source(self) -> "BulkImportJobRequest":
        if not self.job_id and not self.ndjson_path:
            raise ValueError("Either 'job_id' or 'ndjson_path' must be provided.")
        if self.ndjson_path:
            path = self.ndjson_path
            # S3 paths are handled by the S3 backend — no local traversal risk
            if not path.startswith("s3://"):
                allowed_dir = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
                resolved = os.path.realpath(path)
                allowed = os.path.realpath(allowed_dir)
                if not resolved.startswith(allowed + os.sep) and resolved != allowed:
                    raise ValueError(
                        f"ndjson_path must be under the output directory ({allowed_dir})"
                    )
        return self
