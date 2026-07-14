"""Request/response schemas for AI agent endpoints."""

from __future__ import annotations

from typing import Literal

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
    min_field_len: int = Field(
        default=15,
        ge=1,
        le=1000,
        description=(
            "Shortest string value to scan. Default 15 targets free-text "
            "narrative; set 1 to scan every string field (catches short "
            "structured identifiers like SSN/phone/name at the cost of more "
            "NER noise)."
        ),
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
        max_length=60000,
        description="Field-path tree to classify. Paths + JSON value types only "
        "by default; when ``include_values`` is true, each leaf also carries a "
        "truncated SAMPLE value (``path : <type> = value``). Server-derived → "
        "treated as untrusted DATA (sanitized + tag-wrapped before injection).",
    )
    include_values: bool = Field(
        default=False,
        description="When true, ``field_context`` carries patient sample values "
        "so a LOCAL model can judge PII more accurately. The call is then a PHI "
        "payload: the AI local-guard refuses any non-local endpoint (fail-"
        "closed), so values never leave a self-hosted model.",
    )
    model: str = Field(
        default="",
        description="Optional local model override (e.g. 'ollama/gemma3:1b').",
    )
    guidance: str = Field(
        default="",
        max_length=2000,
        description="Optional free-text user guidance on how to treat fields "
        "(e.g. 'pseudonymize all identifiers', 'generalize dates to year'). "
        "Injected as a sanitized system instruction the model follows when "
        "classifying  never mixed into the untrusted field tree.",
    )
    granularity: Literal["values", "whole"] = Field(
        default="values",
        description="How structured (object) fields are treated. 'values' "
        "(default) classifies leaf sub-fields (Patient.name.family) so the FHIR "
        "skeleton is kept and only values are blanked; 'whole' classifies the "
        "parent container (Patient.name) as a single row.",
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


class FieldSketchRequest(BaseModel):
    resource_types: list[str] = Field(
        default_factory=list,
        max_length=64,
        description="Resource types to sample from the configured source FHIR "
        "server (e.g. ['Patient', 'Observation']). Ignored when ``resources`` "
        "is supplied.",
    )
    resources: list[dict] = Field(
        default_factory=list,
        max_length=1000,
        description="Pre-sampled FHIR resources (e.g. the user's uploaded "
        "examples). When present, the server sketches THESE instead of querying "
        "the source server; grouped by ``resourceType`` server-side.",
    )
    n_per_type: int = Field(
        default=25,
        ge=1,
        le=200,
        description="Max resources to sample per type when sampling the source "
        "server. Higher values catch rarer optional fields at more I/O cost.",
    )
    include_values: bool = Field(
        default=False,
        description="When true, each leaf carries a truncated raw SAMPLE value "
        "instead of a PHI-free shape/enum. The returned sketch then contains "
        "PHI, so it is not cached and, when later sent to a model, engages the "
        "local-guard (fail-closed).",
    )


class FieldSketchResponse(BaseModel):
    sketch: str = ""
    types: list[str] = Field(default_factory=list)
    leaf_count: int = 0
    included_count: int = 0
    truncated: bool = False
    source: str = ""  # "fhir" | "inline" | "error"
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
        max_length=60000,
        description="Optional current config YAML for context. Cap matches "
        "``field_context`` (the user's own authored config, already bounded by "
        "the request body cap); a comprehensive field-complete profile easily "
        "exceeds 20k chars. The prompt injects only a truncated prefix "
        "(``MEDANON_AI_CONFIG_CONTEXT_MAX``) to fit the model context.",
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
        max_length=60000,
        description="Field-path tree extracted client-side from uploaded "
        "examples OR sampled from the live FHIR server, so the assistant targets "
        "proposed rules at the FHIRPaths the user actually has. Paths + types "
        "only by default; when ``include_values`` is true each leaf also carries "
        "a truncated SAMPLE value. Server-derived, so treated as untrusted DATA "
        "(sanitized + tag-wrapped before prompt injection).",
    )
    include_values: bool = Field(
        default=False,
        description="When true, ``field_context`` carries patient sample values "
        "so a LOCAL model can ground answers in real data. The call is then a "
        "PHI payload: the AI local-guard refuses any non-local endpoint (fail-"
        "closed), so values never leave a self-hosted model.",
    )
    granularity: Literal["values", "whole"] = Field(
        default="values",
        description="How structured (object) PII fields are treated when "
        "proposing rules. 'values' (default) emits one rule per identifying "
        "leaf sub-field (Patient.name.family) so the FHIR skeleton is preserved "
        "and only values are blanked; 'whole' emits one rule on the parent path "
        "(Patient.name) that removes the entire element.",
    )
