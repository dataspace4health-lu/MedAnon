"""AI Agent API endpoints — /v1/ai/*.

    GET   /v1/ai/status          — provider health + circuit breaker state
    POST  /v1/ai/generate-config — generate config from natural language
    POST  /v1/ai/detect-pii      — scan de-identified resources for PII leaks
    POST  /v1/ai/explain         — explain config rules (supports SSE streaming)
    POST  /v1/ai/compliance      — regulatory gap analysis
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.deps import limiter
from api.schemas.agents import (
    AgentStatusResponse,
    ComplianceRequest,
    ConfigGenerationRequest,
    ConfigGenerationResponse,
    ExplainRequest,
    PiiDetectionRequest,
    PiiDetectionResponse,
)
from api.services.agents import AgentService

router = APIRouter(prefix="/ai", tags=["AI Agents"])
logger = logging.getLogger("medanon")

_service = AgentService()


@router.get("/status", response_model=AgentStatusResponse)
async def ai_status():
    """Return AI provider status, model info, and circuit breaker state."""
    return await _service.get_status()


@router.post("/generate-config", response_model=ConfigGenerationResponse)
@limiter.limit("10/minute")
async def generate_config(body: ConfigGenerationRequest, request: Request):
    """Generate a de-identification config profile from natural-language intent.

    Uses RAG with 7 bundled profiles as few-shot examples. Output is
    validated through the Settings loader before returning.

    Falls back to keyword-matching against bundled profiles when AI is disabled.
    """
    try:
        result = await _service.generate_config(body.prompt, body.regulation)
    except Exception as exc:
        logger.error("ai_generate_config_error: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"Config generation failed: {exc}",
        )
    return result


@router.post("/detect-pii", response_model=PiiDetectionResponse)
@limiter.limit("20/minute")
async def detect_pii(body: PiiDetectionRequest, request: Request):
    """Scan de-identified FHIR resources for residual PII leaks.

    Three detection layers: regex patterns, NER (Presidio), and
    contextual LLM analysis (requires local model via MEDANON_AI_PII_PROVIDER).
    """
    try:
        result = await _service.detect_pii(body.resources, body.use_ai)
    except Exception as exc:
        logger.error("ai_detect_pii_error: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"PII detection failed: {exc}",
        )
    return result


@router.post("/explain")
@limiter.limit("20/minute")
async def explain_config_endpoint(body: ExplainRequest, request: Request):
    """Explain config rules in plain language with regulatory mapping.

    Supports SSE streaming via Accept: text/event-stream header.
    """
    accept = request.headers.get("accept", "")

    if "text/event-stream" in accept:

        async def _sse_generator():
            try:
                from integrations.ai.agents.rule_explainer import (
                    explain_config as _explain,
                )

                gen = _explain(body.yaml_text, streaming=True)
                if hasattr(gen, "__iter__") or hasattr(gen, "__next__"):
                    for chunk in gen:
                        yield f"data: {json.dumps({'text': chunk})}\n\n"
                else:
                    yield f"data: {json.dumps({'text': str(gen)})}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

        return StreamingResponse(
            _sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        result = await _service.explain_config(body.yaml_text, body.regulation)
    except Exception as exc:
        logger.error("ai_explain_error: %s", exc)
        raise HTTPException(status_code=502, detail=f"Explanation failed: {exc}")
    return {"explanation": result}


@router.post("/compliance")
@limiter.limit("10/minute")
async def compliance_analysis(body: ComplianceRequest, request: Request):
    """Regulatory gap analysis for a config profile."""
    try:
        result = await _service.advise_compliance(body.yaml_text, body.regulation)
    except Exception as exc:
        logger.error("ai_compliance_error: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"Compliance analysis failed: {exc}",
        )
    return result
