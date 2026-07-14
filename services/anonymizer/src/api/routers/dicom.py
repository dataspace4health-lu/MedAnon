"""DICOM de-identification endpoints.

POST /process/dicom        — de-identify a single DICOM file
POST /process/dicom/batch  — de-identify multiple DICOM files (multipart/form-data)
"""

import logging
import os
import uuid

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import Response

from api.deps import limiter
from api.routers._format_delivery import deliver_format_output
from api.services.dicom import DicomService
from pipeline.exceptions import OutputBlocked
from pipeline.processor import PiiLeakError

router = APIRouter()
logger = logging.getLogger("medanon")
_service = DicomService()

# DICOM files can be large (CT scans = 50–500 MB). Default 50 MB, overrideable.
DICOM_MAX_BODY_BYTES = int(os.environ.get("DICOM_MAX_BODY_BYTES", 50 * 1024 * 1024))


@router.post("/process/dicom")
@limiter.limit("10/minute")
async def process_dicom(request: Request):
    """De-identify a single DICOM file.

    Accepts raw DICOM bytes as the request body (Content-Type: application/dicom).
    Applies the DICOM PS3.15 Annex E Basic Application Level Confidentiality Profile.
    Returns de-identified DICOM bytes.

    Note: pixel-burned annotations in Pixel Data are not modified by this endpoint.
    """
    body = await request.body()
    if len(body) > DICOM_MAX_BODY_BYTES:
        limit_mb = DICOM_MAX_BODY_BYTES // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"DICOM file exceeds the {limit_mb} MB limit",
        )
    if not body:
        raise HTTPException(status_code=422, detail="Request body is empty")

    # ?config_profile= routes through the full rule engine (DicomAdapter);
    # absent → legacy PS3.15 blanket scrubber (backward-compatible default).
    config_profile = request.query_params.get("config_profile") or None

    try:
        result, manifest = await _service.process_single_with_manifest(
            body, config_profile
        )
    except PiiLeakError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "pii_leak_detected", "message": str(exc)},
        ) from exc
    except OutputBlocked as exc:
        # The score-summary half of the barrier (enforce_output in
        # pipeline.sources.run). Without this clause it fell through to the
        # generic handler below and surfaced as a 500.
        raise HTTPException(
            status_code=422,
            detail={"code": "output_blocked", "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Do not use logger.exception — traceback may contain PHI
        logger.error("dicom_process error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="DICOM processing error") from exc

    delivered = await deliver_format_output(
        result, suffix=".dcm", request=request, resource_type="DICOM", manifest=manifest
    )
    headers = {
        "Content-Disposition": (
            f'attachment; filename="deidentified_{uuid.uuid4().hex[:8]}.dcm"'
        )
    }
    if delivered:
        headers["X-Delivered-To"] = delivered
    return Response(content=result, media_type="application/dicom", headers=headers)


@router.post("/process/dicom/batch")
@limiter.limit("5/minute")
async def process_dicom_batch(request: Request, files: list[UploadFile]):
    """De-identify multiple DICOM files (multipart/form-data).

    Returns a ZIP archive containing de-identified DICOM files.
    Files that cannot be processed are replaced with an error marker (.error.txt).
    """
    if not files:
        raise HTTPException(status_code=422, detail="No files provided")

    file_data: list[tuple[str, bytes]] = []
    total_size = 0
    for upload in files:
        raw = await upload.read()
        total_size += len(raw)
        if total_size > DICOM_MAX_BODY_BYTES * 10:  # 500 MB batch limit
            raise HTTPException(
                status_code=413,
                detail="Batch total size exceeds 500 MB limit",
            )
        file_data.append((upload.filename or f"file_{len(file_data)}.dcm", raw))

    try:
        zip_bytes = await _service.process_batch(file_data)
    except Exception as exc:
        logger.error("dicom_batch error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(
            status_code=500, detail="DICOM batch processing error"
        ) from exc

    job_id = uuid.uuid4().hex[:12]
    delivered = await deliver_format_output(
        zip_bytes, suffix=".zip", request=request, resource_type="DICOM"
    )
    headers = {
        "Content-Disposition": (
            f'attachment; filename="dicom_deidentified_{job_id}.zip"'
        )
    }
    if delivered:
        headers["X-Delivered-To"] = delivered
    return Response(content=zip_bytes, media_type="application/zip", headers=headers)
