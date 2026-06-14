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
    pii_enforcement: dict = Field(default_factory=dict)


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
    include_source_context: bool = Field(
        default=False,
        description="When true, query the configured FHIR_SOURCE_URL for the "
        "resource types/counts present and ground the generated rules in that "
        "actual dataset (no patient data is read).",
    )


class ConfigGenerationResponse(BaseModel):
    model_config = {"extra": "allow"}

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
    model_config = {"extra": "allow"}

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


class FieldScanRequest(BaseModel):
    field_context: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description="PHI-free field-path tree (path: type only, no values) to "
        "classify. Server-derived → treated as untrusted DATA. Must NOT contain "
        "patient values.",
    )
    model: str = Field(
        default="",
        description="Optional local model override (e.g. 'ollama/gemma3:1b').",
    )


class FieldScanResult(BaseModel):
    path: str
    is_pii: bool
    reason: str = ""
    suggested_action: str = ""


class FieldScanResponse(BaseModel):
    results: list[FieldScanResult]
    source: str  # "ai" | "error"
    detail: str = ""


class ChatMessage(BaseModel):
    role: str = Field(..., description="'user' or 'assistant'.")
    content: str = Field(..., max_length=8000)


class ChatIntake(BaseModel):
    """Structured intake collected by the UI before the first chat turn.

    Lets the assistant ground its proposals in the user's stated scope
    (resource types), compliance target, and free-text requirements WITHOUT
    smuggling that metadata into the user's question text. All fields are
    optional; an empty intake injects no extra context.
    """

    resource_types: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="FHIR resource types the user wants rules for "
        "(e.g. ['Patient', 'Observation']). Empty = all types.",
    )
    regulation: str = Field(
        default="",
        max_length=64,
        description="Compliance standard the config must satisfy "
        "(e.g. 'HIPAA', 'GDPR', 'RESEARCH'). Empty/'CUSTOM' = no standard.",
    )
    intent: str = Field(
        default="",
        max_length=1000,
        description="Free-text requirements (e.g. 'keep dates as year-only, "
        "use gPAS for IDs').",
    )


class ChatRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="The user's question about the de-identification config.",
    )
    intake: ChatIntake | None = Field(
        default=None,
        description="Optional structured requirements (resource scope, "
        "regulation, free-text intent) gathered by the UI's guided intake "
        "step. Injected as dedicated system context, not mixed into the "
        "question text.",
    )
    config_yaml: str = Field(
        default="",
        max_length=20000,
        description="Optional current config YAML for context.",
    )
    history: list[ChatMessage] = Field(
        default_factory=list,
        description="Prior turns for multi-turn context (most recent last).",
        max_length=20,
    )
    model: str = Field(
        default="",
        description="Optional model override (e.g. 'ollama/gemma3:1b'). "
        "Empty uses the configured default.",
    )
    include_source_context: bool = Field(
        default=False,
        description="When true, inject a PHI-free summary of the source FHIR "
        "server's resource types/counts so the assistant can tailor answers to "
        "the user's actual data.",
    )
    field_context: str = Field(
        default="",
        max_length=20000,
        description="PHI-free field-path tree (path: type only, no values) "
        "extracted client-side from uploaded examples OR sampled from the live "
        "FHIR server. Lets the assistant target proposed rules at the FHIRPaths "
        "the user actually has. Server-derived, so treated as untrusted DATA "
        "(sanitized + tag-wrapped before prompt injection). Must NOT contain "
        "patient values. Truncated to 16k chars of whole lines if larger.",
    )
