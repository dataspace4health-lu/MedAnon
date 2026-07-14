"""Request/response models for the Trust Gate HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AssessRequest(BaseModel):
    resource: dict[str, Any] | list[dict[str, Any]] = Field(...)
    dataset_id: str = "dataset"
    # Data provider identity — keys the retained assessment history.
    provider_id: str | None = None
    source_types: list[str] = Field(default_factory=lambda: ["fhir"])
    config_profile: str = "auto"
    provenance: dict[str, Any] = Field(default_factory=dict)
    # Selectable audit phases (None/empty → all). See phases.ALL_PHASES.
    phases: list[str] | None = None
    # Sector targets — each {id?, resource_types?, code_systems?} gets its own verdict.
    targets: list[dict[str, Any]] | None = None
    # Declared downstream use — makes the fitness verdict purpose-bound.
    intended_use: str | None = None
    # Declared use case — resolves to a metric subset via use_case_profiles.yaml.
    # Explicit `phases` (if given) still wins.
    use_case: str | None = None
    # Source-of-truth reference: {"records": {"Type/id": {field: expected}}}.
    reference: dict[str, Any] | None = None
    # Transparent-reporting attribution (Phase 4).
    lifecycle_stage: str = "operation"
    org_role: str = "data-receiving"
    # Opt-in Implementation-Guide conformance (e.g. "us_core"). None → IG check NA.
    ig: str | None = None
    # Optional idempotency key: a retry/replica race with the same key collapses
    # onto one persisted assessment row instead of duplicating it.
    idempotency_key: str | None = None
    # Caller-supplied custom expectations (same declarative schema as checks.yaml
    # plausibility rules). Merged into the rule set for this request only.
    custom_rules: list[dict[str, Any]] | None = None
    # Skip ONLY the slow external FHIR-validator calls (structural/profile/IG) while
    # keeping the in-process structural checks. Set false for high-volume scans.
    external_validation: bool = True


class BatchAssessRequest(BaseModel):
    resources: list[dict[str, Any]] = Field(..., max_length=50000)
    dataset_id: str = "dataset"
    provider_id: str | None = None
    # Bundle entry.fullUrl values, so referential integrity can resolve absolute /
    # urn:uuid references (NA without them). Carried separately because the batch
    # API takes a flat resource list, not a Bundle.
    full_urls: list[str] | None = None
    source_types: list[str] = Field(default_factory=lambda: ["fhir"])
    config_profile: str = "auto"
    provenance: dict[str, Any] = Field(default_factory=dict)
    phases: list[str] | None = None
    targets: list[dict[str, Any]] | None = None
    intended_use: str | None = None
    use_case: str | None = None
    reference: dict[str, Any] | None = None
    lifecycle_stage: str = "operation"
    org_role: str = "data-receiving"
    ig: str | None = None
    idempotency_key: str | None = None
    custom_rules: list[dict[str, Any]] | None = None
    external_validation: bool = True


class OmopAssessRequest(BaseModel):
    # Provider submits OMOP rows grouped by table, optionally with a column map...
    tables: dict[str, list[dict[str, Any]]] | None = None
    mapping: dict[str, dict[str, str]] | None = None
    # ...or FHIR resources, which are mapped to the OMOP core structurally.
    resources: list[dict[str, Any]] | None = None
    dataset_id: str = "dataset"
    provider_id: str | None = None
    source_types: list[str] = Field(default_factory=lambda: ["omop"])
    config_profile: str = "auto"
    provenance: dict[str, Any] = Field(default_factory=dict)
    intended_use: str | None = None
    use_case: str | None = None
    lifecycle_stage: str = "operation"
    org_role: str = "data-receiving"
    idempotency_key: str | None = None


class FindingCreate(BaseModel):
    dataset_id: str
    check_id: str = ""
    severity: str = "major"
    assessment_id: str | None = None
    note: str = ""
    owner: str | None = None


class FindingTransition(BaseModel):
    status: str | None = None
    root_cause: str | None = None
    owner: str | None = None
    note: str | None = None


class SqlConnectRequest(BaseModel):
    driver: str = Field(..., description="postgresql | mysql | sqlite")
    host: str = Field("", description="Database host (empty for SQLite)")
    port: int | None = Field(
        None, description="Database port (uses driver default if omitted)"
    )
    database: str = Field(..., description="Database name or SQLite file path")
    username: str = Field("", description="Database username")
    password: str = Field("", description="Database password")
    query: str = Field(..., description="SELECT query to run (DDL/DML rejected)")
    table_name: str | None = Field(
        None, description="Key for the result table (defaults to 'query_result')"
    )
    mapping: dict[str, dict[str, str]] | None = Field(
        None, description="OMOP column mapping: {table: {omop_col: source_col}}"
    )
    dataset_id: str | None = None
    provider_id: str | None = None
    config_profile: str | None = None
    provenance: str | None = None
    intended_use: str | None = None
    lifecycle_stage: str | None = None
    org_role: str | None = None
    use_case: str | None = None


class SqlTablesRequest(BaseModel):
    driver: str
    host: str = ""
    port: int | None = None
    database: str
    username: str = ""
    password: str = ""
