"""Scoring API endpoints.

POST  /score              — score a single de-identified resource (ad-hoc)
POST  /jobs/{job_id}/score — trigger on-demand scoring for a completed job
GET   /jobs/{job_id}/score — retrieve cached score for a job
GET   /jobs/{job_id}/score/report — retrieve Markdown audit report
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from api.schemas.scoring import (
    FieldClassifyRequest,
    FieldClassifyResponse,
    ScoreResourceRequest,
)
from api.services.scoring import ScoringService

router = APIRouter()
logger = logging.getLogger("medanon")

_service = ScoringService()


@router.post("/classify-fields", response_model=FieldClassifyResponse)
async def classify_fields(req: FieldClassifyRequest):
    """Classify FHIR leaf paths as direct / quasi / non identifiers.

    Deterministic and model-free: reuses ``HIPAA_SENSITIVE_PATHS`` and the
    severity grading the structural output gate enforces, so the labels match
    what the engine actually blocks. This is the authoritative classification
    the Resource Explorer consumes for its per-field direct/quasi badges.
    """
    from pipeline.field_classification import classify_paths

    return FieldClassifyResponse(
        classes=classify_paths(req.resource_type, req.paths)
    )


@router.post("/score")
async def score_resource(
    req: ScoreResourceRequest,
    include_audit: bool = Query(
        default=False, description="Include Markdown audit report in response"
    ),
):
    """Score a de-identified FHIR resource.

    Evaluates privacy risk (hard constraint), utility preservation, and
    quality of the de-identification pipeline output. Returns a composite
    score with per-module evidence.
    """
    # Build a minimal settings-like object for rule coverage checks
    settings = None
    if req.settings_rules:

        class _MinimalSettings:
            def __init__(self, rules):
                self.rules = rules

        settings = _MinimalSettings(req.settings_rules)

    try:
        result = await asyncio.to_thread(
            _service.score_resource,
            original=req.original,
            deidentified=req.deidentified,
            manifest_entries=req.manifest_entries,
            config_profile=req.config_profile,
            settings=settings,
            include_audit=include_audit,
        )
    except Exception as exc:
        logger.error("score_error: %s", exc)
        raise HTTPException(status_code=500, detail="Scoring failed")
    return result


@router.post("/jobs/{job_id}/score")
async def score_job(job_id: str, config_profile: str | None = None):
    """Trigger on-demand scoring for a completed job.

    Reads the job's NDJSON result file, extracts transformation manifests
    from meta.tag, scores each resource, and returns a batch-level aggregate.
    The result is cached in the job's checkpoint for subsequent GET requests.
    """
    from domain.jobs import JobNotFound, JobNotComplete

    try:
        result = await asyncio.to_thread(
            _service.score_job,
            job_id,
            config_profile,
        )
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except JobNotComplete:
        raise HTTPException(
            status_code=409,
            detail="Job not yet complete — scoring requires a finished job",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("score_job_error job=%s: %s", job_id, exc)
        raise HTTPException(status_code=500, detail="Scoring failed")
    return result


@router.get("/jobs/{job_id}/score/report", response_class=PlainTextResponse)
async def get_job_score_report(job_id: str):
    """Return the Markdown audit report for a scored job.

    The report is generated automatically when you call
    ``POST /v1/jobs/{job_id}/score`` and written to
    ``/output/{job_id}_score_audit.md``.  It explains:

    - Why each score (composite / utility / quality) is what it is
    - Which HIPAA-sensitive paths were not covered by rules
    - Which text-risk patterns were detected in narrative fields
    - Which rules fired vs. which were missed
    - Per-resource-type PASS/FAIL breakdown
    - Priority-ordered recommendations with config YAML snippets
    """
    from domain.jobs import JobNotFound

    try:
        report = await asyncio.to_thread(_service.get_audit_report, job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return report


@router.get("/jobs/{job_id}/score")
async def get_job_score(job_id: str):
    """Retrieve the cached score summary for a job.

    Returns the batch-level scoring results if scoring has been previously
    triggered via POST. If not yet scored, returns a 200 with computed=False.
    """
    from domain.jobs import JobNotFound

    try:
        result = await asyncio.to_thread(_service.get_job_score, job_id)
    except JobNotFound:
        raise HTTPException(status_code=404, detail="Job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return result
