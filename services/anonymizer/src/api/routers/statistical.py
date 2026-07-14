"""Statistical-format export endpoint: /export/statistical (D7.2 §5.5.4).

Answers an EHDS *data request* with an anonymised aggregate (group-by counts)
instead of record-level data. Disclosure control is small-cell suppression and/or
differential privacy (see :mod:`analytics.statistical`). Aggregate-only, so it
runs locally regardless of any microservice split.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from api.deps import MAX_BODY_BYTES, limiter

router = APIRouter()
logger = logging.getLogger("medanon")


def _parse_group_by(spec: str) -> list[dict]:
    """Parse ``path[:kind],...`` into group-by descriptors.

    A leading ``ResourceType.`` on the first path also sets the resource_type
    filter so only matching resources are aggregated.
    """
    group_by: list[dict] = []
    for i, part in enumerate(p.strip() for p in spec.split(",") if p.strip()):
        path, _, kind = part.partition(":")
        entry: dict = {"path": path, "kind": kind or "raw"}
        if i == 0 and "." in path:
            entry["resource_type"] = path.split(".", 1)[0]
        group_by.append(entry)
    if not group_by:
        raise ValueError("group_by must list at least one path")
    return group_by


@router.post("/export/statistical")
@limiter.limit("30/minute")
async def export_statistical(
    request: Request,
    group_by: str = Query(
        ...,
        description="Comma-separated group-by QIs as 'path[:kind]', e.g. "
        "'Patient.gender,Patient.birthDate:year,Patient.address.postalCode:zip3'. "
        "kinds: raw, year, month, decade, zip3, zip2.",
    ),
    min_cell: int = Query(
        5,
        ge=0,
        le=1000,
        description="Small-cell suppression threshold (0 disables). Cells below "
        "this count are suppressed; default 5 (statistical-disclosure floor).",
    ),
    epsilon: float | None = Query(
        None,
        gt=0,
        description="If set, add epsilon-DP Laplace noise to every cell before "
        "suppression, with budget accounting.",
    ),
    delta: float | None = Query(
        None,
        ge=0,
        lt=1,
        description="DP delta (recorded alongside epsilon; Laplace is pure-eps).",
    ),
    unbounded: bool = Query(
        True,
        description="DP neighbour model: add/remove one record (True, sensitivity 1) "
        "vs change one record (False, sensitivity 2).",
    ),
):
    """Aggregate a de-identified FHIR payload into an anonymised statistical release."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    content_type = request.headers.get("content-type", "")

    try:
        group_by_spec = _parse_group_by(group_by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    dp_params = None
    if epsilon is not None:
        dp_params = {"epsilon": epsilon, "unbounded": unbounded}
        if delta is not None:
            dp_params["delta"] = delta

    try:
        from pipeline.io_formats import parse_payload_bytes
        from api.deps import _unwrap_to_resources
        from analytics.statistical import build_statistical_release

        payload = parse_payload_bytes(body, content_type=content_type)
        resources = _unwrap_to_resources(payload)
        release = build_statistical_release(
            resources,
            group_by=group_by_spec,
            min_cell=min_cell,
            dp_params=dp_params,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"Could not parse input: {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("export_statistical error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Statistical export error") from exc

    return JSONResponse(content=release)
