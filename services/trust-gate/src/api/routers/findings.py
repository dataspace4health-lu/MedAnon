"""Remediation-findings endpoints (the PDSA loop): list / create / get / transition."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from api.deps import require_findings
from api.schemas import FindingCreate, FindingTransition

router = APIRouter()


@router.get("/v1/findings")
def list_findings(
    dataset_id: str | None = None, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    store = require_findings()
    return {"findings": store.list(dataset_id=dataset_id, status=status, limit=limit)}


@router.post("/v1/findings")
def create_finding(req: FindingCreate) -> dict[str, Any]:
    store = require_findings()
    fid = store.create(req.model_dump())
    return store.get(fid)


@router.get("/v1/findings/{finding_id}")
def get_finding(finding_id: str) -> dict[str, Any]:
    store = require_findings()
    finding = store.get(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return finding


@router.post("/v1/findings/{finding_id}/transition")
def transition_finding(finding_id: str, req: FindingTransition) -> dict[str, Any]:
    store = require_findings()
    if req.status is not None and req.status not in ("open", "triaged", "resolved"):
        raise HTTPException(status_code=400, detail="invalid status")
    updated = store.transition(finding_id, **req.model_dump())
    if updated is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return updated


@router.get("/v1/datasets/{dataset_id}/findings")
def dataset_findings(
    dataset_id: str, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    store = require_findings()
    return {
        "dataset_id": dataset_id,
        "findings": store.list(dataset_id=dataset_id, status=status, limit=limit),
    }
