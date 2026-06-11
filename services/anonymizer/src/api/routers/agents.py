"""AI Agent API endpoints — /v1/ai/*.

GET   /v1/ai/status          — provider health + circuit breaker state
POST  /v1/ai/generate-config — generate config from natural language
POST  /v1/ai/detect-pii      — scan de-identified resources for PII leaks
POST  /v1/ai/explain         — explain config rules (supports SSE streaming)
POST  /v1/ai/chat            — conversational Q&A about a config (SSE stream)
POST  /v1/ai/compliance      — regulatory gap analysis
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.deps import limiter
from api.schemas.agents import (
    AgentStatusResponse,
    ChatRequest,
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

# Marker distinguishing "queue empty, keep waiting" from a real None sentinel.
_SENTINEL_EMPTY = object()


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
            status_code=502,
            detail=f"Config generation failed: {exc}",
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
            status_code=502,
            detail=f"PII detection failed: {exc}",
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
        import asyncio as _asyncio
        import queue as _queue

        async def _sse_generator():
            # Run the synchronous litellm generator in a thread to avoid blocking
            # the event loop.  Chunks are passed back via a thread-safe queue;
            # sentinel None signals completion, Exception signals failure.
            chunk_queue: _queue.Queue = _queue.Queue()

            def _produce():
                try:
                    from integrations.ai.agents.rule_explainer import (
                        explain_config as _explain,
                    )

                    gen = _explain(body.yaml_text, streaming=True)
                    if hasattr(gen, "__iter__") or hasattr(gen, "__next__"):
                        for chunk in gen:
                            chunk_queue.put(chunk)
                    else:
                        chunk_queue.put(str(gen))
                except Exception as exc:
                    chunk_queue.put(exc)
                finally:
                    chunk_queue.put(None)  # sentinel

            loop = _asyncio.get_event_loop()
            await loop.run_in_executor(None, _produce)

            while True:
                item = await loop.run_in_executor(None, chunk_queue.get)
                if item is None:
                    yield "data: [DONE]\n\n"
                    break
                if isinstance(item, Exception):
                    yield f"data: {json.dumps({'error': str(item)})}\n\n"
                    break
                yield f"data: {json.dumps({'text': item})}\n\n"

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


@router.post("/chat")
@limiter.limit("30/minute")
async def chat_config_endpoint(body: ChatRequest, request: Request):
    """Conversational Q&A about the de-identification config being built.

    Streams the answer as SSE (the UI consumes ``data: {"text": ...}`` chunks
    terminated by ``data: [DONE]``). The optional ``model`` field selects which
    local model answers (e.g. ``ollama/gemma3:1b`` vs the medical model).
    """
    import asyncio as _asyncio
    import queue as _queue
    import threading as _threading

    history = [m.model_dump() for m in body.history]

    async def _sse_generator():
        # Produce chunks in a dedicated daemon thread (litellm's streaming
        # generator is synchronous and blocking). The drain loop polls the
        # queue with a short timeout via the event loop's executor — using a
        # dedicated producer thread (not the shared executor) avoids starving
        # the single default executor worker, which would otherwise deadlock
        # the get() call against the produce() call.
        chunk_queue: _queue.Queue = _queue.Queue()
        loop = _asyncio.get_event_loop()

        def _produce():
            try:
                from integrations.ai.agents.config_chat import chat_config

                gen = chat_config(
                    body.question,
                    config_yaml=body.config_yaml,
                    history=history,
                    model=body.model,
                    streaming=True,
                )
                if hasattr(gen, "__iter__") or hasattr(gen, "__next__"):
                    for chunk in gen:
                        chunk_queue.put(chunk)
                else:
                    chunk_queue.put(str(gen))
            except Exception as exc:  # noqa: BLE001 — surfaced to the client
                chunk_queue.put(exc)
            finally:
                chunk_queue.put(None)  # completion sentinel

        producer = _threading.Thread(target=_produce, daemon=True)
        producer.start()

        def _next():
            try:
                return chunk_queue.get(timeout=0.5)
            except _queue.Empty:
                return _SENTINEL_EMPTY

        while True:
            item = await loop.run_in_executor(None, _next)
            if item is _SENTINEL_EMPTY:
                # Keep the connection alive while the model is still producing.
                yield ": keep-alive\n\n"
                continue
            if item is None:
                yield "data: [DONE]\n\n"
                break
            if isinstance(item, Exception):
                yield f"data: {json.dumps({'error': str(item)})}\n\n"
                break
            yield f"data: {json.dumps({'text': item})}\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/compliance")
@limiter.limit("10/minute")
async def compliance_analysis(body: ComplianceRequest, request: Request):
    """Regulatory gap analysis for a config profile."""
    try:
        result = await _service.advise_compliance(body.yaml_text, body.regulation)
    except Exception as exc:
        logger.error("ai_compliance_error: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=f"Compliance analysis failed: {exc}",
        )
    return result
