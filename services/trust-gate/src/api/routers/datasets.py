"""Read endpoints over retained assessments: provider history, dataset trend/audit,
a single assessment, and the GDPR Art. 30 processing record."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.deps import require_store

router = APIRouter()


@router.get("/v1/providers/{provider_id}/assessments")
def provider_assessments(provider_id: str, limit: int = 50) -> dict[str, Any]:
    store = require_store()
    return {
        "provider_id": provider_id,
        "assessments": store.list_assessments(provider_id, limit),
    }


@router.get("/v1/datasets/{dataset_id}/history")
def dataset_history(dataset_id: str, limit: int = 50) -> dict[str, Any]:
    store = require_store()
    return {"dataset_id": dataset_id, "history": store.history(dataset_id, limit)}


@router.get("/v1/datasets/{dataset_id}/trend")
def dataset_trend(dataset_id: str, limit: int = 100) -> dict[str, Any]:
    store = require_store()
    return {"dataset_id": dataset_id, "trend": store.trend(dataset_id, limit)}


@router.get("/v1/datasets/{dataset_id}/audit")
def dataset_audit(dataset_id: str, limit: int = 100) -> dict[str, Any]:
    """Append-only ALCOA++ assessment audit trail for a dataset (Phase 6.3)."""
    store = require_store()
    return {"dataset_id": dataset_id, "audit": store.audit(dataset_id, limit)}


@router.get("/v1/assessments/{assessment_id}")
def get_assessment(assessment_id: str) -> dict[str, Any]:
    store = require_store()
    passport = store.get(assessment_id)
    if passport is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    return passport


@router.get("/v1/datasets/{dataset_id}/gdpr-record")
def dataset_gdpr_record(
    dataset_id: str, assessment_id: str | None = None
) -> dict[str, Any]:
    """GDPR Art. 30 record of processing activities for the read-only QC evaluation
    (EU compliance): the latest passport's provenance + reproducibility + ALCOA++
    trail, assembled into an EU-audit-ready record. Pass ``assessment_id`` to pin a
    specific assessment."""
    from eu_audit import build_processing_record

    store = require_store()
    if assessment_id is None:
        # history() returns summary rows; resolve the latest id to the full passport
        # (the summary omits the evaluation/reproducibility block the record needs).
        hist = store.history(dataset_id, limit=1)
        assessment_id = hist[0].get("id") if hist else None
    passport = store.get(assessment_id) if assessment_id else None
    if passport is None:
        raise HTTPException(status_code=404, detail="no assessment found for dataset")
    return build_processing_record(passport, store.audit(dataset_id))
