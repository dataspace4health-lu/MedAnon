"""Request/response schemas for the scoring API endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FieldClassifyRequest(BaseModel):
    """Request body for POST /v1/classify-fields."""

    resource_type: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="FHIR resource type the paths belong to (e.g. 'Patient').",
    )
    paths: list[str] = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="FHIR leaf paths to classify (may carry .where()/[i]).",
    )


class FieldClassifyResponse(BaseModel):
    """Response for POST /v1/classify-fields: {path: 'direct'|'quasi'|'non'}."""

    classes: dict[str, str] = Field(default_factory=dict)


class ScoreResourceRequest(BaseModel):
    """Request body for POST /v1/score."""

    original: dict | None = Field(
        default=None,
        description="Original FHIR resource (before de-identification). Optional — enables field retention and temporal consistency checks.",
    )
    deidentified: dict = Field(
        ...,
        description="De-identified FHIR resource to score.",
    )
    manifest_entries: list[dict] = Field(
        default_factory=list,
        description="Transformation manifest entries ({rule, action, path}).",
    )
    config_profile: str = Field(
        default="auto",
        description="Config profile name (for labelling the score result).",
    )
    settings_rules: list[dict] | None = Field(
        default=None,
        description="Optional rule definitions for rule coverage checks.",
    )


class EvidenceResponse(BaseModel):
    check: str
    value: float
    details: dict = Field(default_factory=dict)
    severity: str = "info"


class PrivacyDecisionResponse(BaseModel):
    risk_score: float
    passed: bool
    threshold: float
    attacker_risk: float
    identifier_risk: float
    text_risk: float
    evidence: list[EvidenceResponse] = Field(default_factory=list)


class ModuleScoreResponse(BaseModel):
    name: str
    score: float
    evidence: list[EvidenceResponse] = Field(default_factory=list)
    gates_applied: list[str] = Field(default_factory=list)


class ScoreResultResponse(BaseModel):
    composite: float
    decision: str
    privacy: PrivacyDecisionResponse
    utility: ModuleScoreResponse | None = None
    quality: ModuleScoreResponse | None = None
    resource_type: str
    resource_id: str | None = None
    scored_at: str
    config_profile: str
