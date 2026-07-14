"""Assessment endpoints: single resource/Bundle, OMOP, and batch."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from cdm.fhir_to_omop import fhir_to_omop
from cdm.tabular_to_omop import tabular_to_omop
from engine import assess_omop
from label import build_label
from metric_catalog import resolve_critical_to_quality

from api.metrics import DECISIONS, LATENCY, REQUESTS
from api.schemas import AssessRequest, BatchAssessRequest, OmopAssessRequest
from api.service import flatten, persist, run_assessment

_log = logging.getLogger("trust_gate")
router = APIRouter()


@router.post("/v1/trust/assess")
def assess_endpoint(req: AssessRequest) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        resources, full_urls = flatten(req.resource)
        result = run_assessment(resources, req, full_urls=full_urls)
    except HTTPException:
        REQUESTS.labels(endpoint="assess", outcome="error").inc()
        raise  # client errors (e.g. invalid custom_rules → 422) keep their status
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="assess", outcome="error").inc()
        _log.exception("assess failed")
        raise HTTPException(status_code=500, detail=f"assess failed: {exc}") from exc
    REQUESTS.labels(endpoint="assess", outcome="ok").inc()
    LATENCY.labels(endpoint="assess").observe(time.monotonic() - t0)
    return result


@router.post("/v1/trust/assess/omop")
def assess_omop_endpoint(req: OmopAssessRequest) -> dict[str, Any]:
    """Assess an OMOP CDM dataset (tabular/SQL rows or FHIR mapped to OMOP)."""
    t0 = time.monotonic()
    try:
        if req.tables:
            omop = tabular_to_omop(req.tables, req.mapping)
        elif req.resources:
            omop = fhir_to_omop([r for r in req.resources if isinstance(r, dict)])
        else:
            raise HTTPException(
                status_code=400,
                detail="provide 'tables' (OMOP rows) or 'resources' (FHIR)",
            )
        passport = assess_omop(
            omop,
            dataset_id=req.dataset_id,
            source_types=req.source_types,
            config_profile=req.config_profile,
            provenance=req.provenance,
            intended_use=req.intended_use,
            lifecycle_stage=req.lifecycle_stage,
            org_role=req.org_role,
            critical_check_ids=resolve_critical_to_quality(req.use_case),
        )
        DECISIONS.labels(decision=passport.decision).inc()
        result = passport.to_dict()
        result["label"] = build_label(result)
        persist(result, req.provider_id, getattr(req, "idempotency_key", None))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="assess_omop", outcome="error").inc()
        _log.exception("assess_omop failed")
        raise HTTPException(status_code=500, detail=f"assess failed: {exc}") from exc
    REQUESTS.labels(endpoint="assess_omop", outcome="ok").inc()
    LATENCY.labels(endpoint="assess_omop").observe(time.monotonic() - t0)
    return result


@router.post("/v1/trust/assess/batch")
def assess_batch_endpoint(req: BatchAssessRequest) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        result = run_assessment(
            [r for r in req.resources if isinstance(r, dict)],
            req,
            full_urls=req.full_urls,
        )
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="assess_batch", outcome="error").inc()
        _log.exception("assess_batch failed")
        raise HTTPException(status_code=500, detail=f"assess failed: {exc}") from exc
    REQUESTS.labels(endpoint="assess_batch", outcome="ok").inc()
    LATENCY.labels(endpoint="assess_batch").observe(time.monotonic() - t0)
    return result
