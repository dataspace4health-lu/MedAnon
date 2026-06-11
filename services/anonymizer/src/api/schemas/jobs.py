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


class RiskDrivenExportJobRequest(BaseModel):
    """Request body for POST /v1/jobs/risk-driven-export.

    Fetches FHIR resources, builds a dataset-wide QI distribution, searches
    the generalization lattice for the minimal generalization that achieves
    the target k-anonymity guarantee, and outputs de-identified NDJSON with
    suppressed patients removed.

    Requires ``MEDANON_STAGING_DB_URL`` to be configured.

    The ``config_profile`` must reference a profile with a ``privacy_model``
    block, or ``privacy_model`` must be supplied inline here to override.
    """

    server_url: str | None = None
    resource_type: str | None = Field(
        default=None,
        description="Single FHIR resource type to export (e.g. 'Patient'). "
        "Omit to export all types from the server's CapabilityStatement.",
    )
    type_filter: str | None = Field(
        default=None,
        description="Comma-separated resource types to export. "
        "Mutually exclusive with resource_type.",
    )
    since: str | None = Field(
        default=None,
        description="ISO-8601 timestamp; only resources updated after this date are fetched.",
    )
    token: str | None = None
    timeout: float = Field(default=30.0, ge=1.0, le=300.0)
    config_profile: str = Field(
        default="config_k_anonymity",
        description="Config profile name.  Must include a privacy_model block, "
        "or supply privacy_model inline.",
    )
    privacy_model: dict | None = Field(
        default=None,
        description="Inline privacy_model override.  Merged over the profile's block. "
        "Useful for overriding target_k, max_suppression, or quasi_identifiers "
        "without creating a new profile.",
    )


class UploadToTargetRequest(BaseModel):
    """Request body for POST /jobs/{job_id}/upload-to-target.

    Keeping credentials in the request body (not query params) prevents
    target_token from appearing in server access logs and proxy histories.
    """

    target_url: str | None = Field(
        default=None,
        description="Target FHIR server URL. Falls back to FHIR_TARGET_URL env var when not provided.",
    )
    target_token: str | None = Field(
        default=None,
        description="Bearer token for the target FHIR server. Falls back to FHIR_TARGET_TOKEN env var when not provided.",
    )


class SqlExportJobRequest(BaseModel):
    """Request body for POST /jobs/sql-export.

    De-identifies selected tables of a saved SQL source connection to files
    (one per table) in a result ZIP. Credentials are never sent here — only the
    saved ``connection_id`` (resolved from the encrypted store at run time).
    Provide either a saved ``config_profile`` (with ``table:``/``column:`` rules)
    or inline ``rules``.
    """

    connection_id: str = Field(description="Saved SQL source connection id.")
    tables: list[str] = Field(min_length=1, description="Tables to export.")
    schema_name: str = Field(
        default="public", alias="schema", description="Source schema."
    )
    output_format: str = Field(default="csv", description="csv | ndjson | parquet.")
    config_profile: str = Field(
        default="auto",
        description="Saved profile whose table:/column: rules to apply.",
    )
    rules: list[dict] | None = Field(
        default=None,
        description="Inline column rules; take precedence over config_profile.",
    )
    chunk_size: int = Field(default=1000, ge=1, le=100000)

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _check_format(self):
        if self.output_format.lower() not in ("csv", "ndjson", "parquet"):
            raise ValueError("output_format must be csv, ndjson, or parquet")
        return self
