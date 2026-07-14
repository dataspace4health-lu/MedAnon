"""Transformation-passport report endpoints (D7.2 §5.5.1 / EHDS Art 79).

Read-only access to the durable passport reports a risk-driven export produced.
Requires a Postgres report store (``MEDANON_APP_DB_URL``); without it the list
is empty (per-job passports remain visible via ``GET /v1/jobs/{id}``). Reports
are anonymous by construction  no PHI.
"""

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.deps import limiter
from pipeline.reports import get_passport_store

router = APIRouter()
logger = logging.getLogger("medanon")


@router.get("/reports")
@limiter.limit("60/minute")
async def list_reports(request: Request):
    """List recent transformation-passport reports (index rows)."""
    store = get_passport_store()
    if store is None:
        return JSONResponse(content=[])
    try:
        return JSONResponse(content=store.list())
    except Exception as exc:  # noqa: BLE001
        logger.error("list_reports error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Report store error") from exc


@router.get("/reports/{job_id}")
@limiter.limit("60/minute")
async def get_report(job_id: str, request: Request):
    """Fetch the full Transformation Passport for *job_id*."""
    store = get_passport_store()
    if store is None:
        raise HTTPException(status_code=404, detail="report store not configured")
    try:
        passport = store.get(job_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("get_report error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Report store error") from exc
    if passport is None:
        raise HTTPException(status_code=404, detail=f"no report for job {job_id}")
    return JSONResponse(content=passport)
