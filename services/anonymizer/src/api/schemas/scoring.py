"""Request/response schemas for the scoring API endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


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
