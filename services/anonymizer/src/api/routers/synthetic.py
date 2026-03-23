"""Synthetic data generation endpoint: /generate/synthetic."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from pipeline.io_formats import parse_payload_bytes
from analytics.synthetic import generate_synthetic_patients as _generate_synthetic_patients
from analytics.synthetic import generate_synthetic_conditions as _generate_synthetic_conditions

# SDV is optional — graceful fallback to stdlib generator.
try:
    from analytics.synthetic_sdv import (
        SDV_AVAILABLE,
        generate_synthetic_patients_sdv as _generate_synthetic_patients_sdv,
        generate_synthetic_conditions_sdv as _generate_synthetic_conditions_sdv,
    )
except ImportError:
    SDV_AVAILABLE = False

from api.deps import MAX_BODY_BYTES, limiter, _unwrap_to_resources

router = APIRouter()
logger = logging.getLogger("medanon")


@router.post("/generate/synthetic")
@limiter.limit("30/minute")
async def generate_synthetic(
    request: Request,
    count: int = Query(100, ge=1, le=10000,
                       description="Number of synthetic Patient resources to generate"),
    seed: int | None = Query(None, description="Random seed for reproducibility"),
    engine: str = Query("auto", description="Engine: 'sdv' (GaussianCopula), 'stdlib' (weighted sampling), or 'auto' (SDV if available, else stdlib)"),
    include_conditions: bool = Query(False, description="Also generate synthetic Conditions linked to the synthetic Patients"),
    count_per_patient: int = Query(2, ge=0, le=10, description="Max Conditions per Patient (when include_conditions=true)"),
):
    """Generate synthetic FHIR Patient resources from a de-identified input dataset.

    Accepts de-identified FHIR resources (NDJSON, JSON Bundle, or XML),
    extracts statistical distributions, and returns *count* synthetic Patient
    resources (plus optional Conditions) as NDJSON.

    Engine choices:
    - **auto** (default): Use SDV if installed, otherwise fall back to stdlib.
    - **sdv**: GaussianCopulaSynthesizer — learns multivariate correlations.
    - **stdlib**: Weighted per-attribute sampling — zero dependencies, always available.

    Synthetic resources are tagged with the ``SYN`` code in ``meta.tag`` so
    downstream systems can distinguish them from real de-identified data.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")
    try:
        payload = parse_payload_bytes(body, content_type=content_type)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse input: {exc}") from exc

    resources = _unwrap_to_resources(payload)
    patients = [r for r in resources if r.get("resourceType") == "Patient"]

    if not patients:
        raise HTTPException(
            status_code=422,
            detail="No Patient resources found in input — provide de-identified Patient FHIR resources",
        )

    # Resolve engine choice.
    use_sdv = False
    if engine == "sdv":
        if not SDV_AVAILABLE:
            raise HTTPException(
                status_code=422,
                detail="SDV engine requested but sdv package is not installed. Install with: pip install -r requirements-sdv.txt",
            )
        use_sdv = True
    elif engine == "auto":
        use_sdv = SDV_AVAILABLE
    elif engine != "stdlib":
        raise HTTPException(status_code=422, detail=f"Unknown engine '{engine}'. Choose: auto, sdv, stdlib")

    try:
        if use_sdv:
            synthetic = await asyncio.to_thread(
                _generate_synthetic_patients_sdv, patients, count=count, seed=seed
            )
        else:
            synthetic = _generate_synthetic_patients(patients, count=count, seed=seed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("generate_synthetic: unexpected error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Synthetic generation error") from exc

    # Optionally generate linked Conditions.
    synthetic_conditions: list[dict] = []
    if include_conditions:
        conditions = [r for r in resources if r.get("resourceType") == "Condition"]
        if conditions:
            try:
                if use_sdv:
                    synthetic_conditions = await asyncio.to_thread(
                        _generate_synthetic_conditions_sdv,
                        conditions, synthetic, count_per_patient=count_per_patient, seed=seed,
                    )
                else:
                    synthetic_conditions = _generate_synthetic_conditions(
                        conditions, synthetic, count_per_patient=count_per_patient, seed=seed,
                    )
            except ValueError:
                pass  # Silently skip if conditions input is insufficient.

    engine_used = "sdv" if use_sdv else "stdlib"

    async def _stream():
        for patient in synthetic:
            yield json.dumps(patient) + "\n"
        for condition in synthetic_conditions:
            yield json.dumps(condition) + "\n"

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers={"X-Synthetic-Engine": engine_used},
    )
