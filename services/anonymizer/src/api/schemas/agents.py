"""Request/response schemas for AI agent endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class AgentStatusResponse(BaseModel):
    enabled: bool
    provider: str = ""
    model: str = ""
    api_base: str = ""
    circuit_breaker: dict = Field(default_factory=dict)
    cache_size: int = 0


class ConfigGenerationRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="Natural language description of de-identification requirements.",
    )
    regulation: str = Field(
        default="",
        description="Target compliance framework (e.g., 'HIPAA', 'GDPR').",
    )


class ConfigGenerationResponse(BaseModel):
    yaml: str
    valid: bool
    validation_error: str = ""
    source: str  # "ai" | "fallback" | "error"


class PiiDetectionRequest(BaseModel):
    resources: list[dict] = Field(
        ...,
        min_length=1,
        max_length=100,
        description="De-identified FHIR resources to scan for residual PII.",
    )
    use_ai: bool = Field(
        default=True,
        description="Enable AI-powered contextual analysis (requires local model).",
    )


class PiiDetectionResponse(BaseModel):
    detections: list[dict]
    summary: dict
    layers_used: list[str]


class ExplainRequest(BaseModel):
    yaml_text: str = Field(
        ...,
        min_length=10,
        description="YAML config to explain.",
    )
    regulation: str = Field(
        default="",
        description="Optional regulation for alignment analysis.",
    )


class ComplianceRequest(BaseModel):
    yaml_text: str = Field(
        ...,
        min_length=10,
        description="YAML config to analyse.",
    )
    regulation: str = Field(
        ...,
        min_length=2,
        description="Target regulation (e.g., 'HIPAA Safe Harbor', 'GDPR').",
    )
