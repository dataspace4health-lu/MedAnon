"""Tabular (CSV / Excel / Parquet) de-identification endpoints.

POST /process/tabular/inspect — preview columns + sample values (no de-id)
POST /process/tabular         — de-identify by column rule, return same format

De-identification targets columns via the ``column:<name>`` matcher.  Rules
come from a saved ``config_profile`` OR from an inline ``rules`` JSON query
param authored by the UI column-mapper.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from api.deps import MAX_BODY_BYTES, limiter
from api.services.tabular import TabularService
from pipeline.exceptions import NormalizationError, OutputBlocked
from pipeline.processor import PiiLeakError

router = APIRouter()
logger = logging.getLogger("medanon")

_service = TabularService()

_MEDIA = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "parquet": "application/vnd.apache.parquet",
}


def _parse_inline_rules(raw_param: str | None) -> list[dict] | None:
    """Parse the ``rules`` query param (JSON) into ``column:`` rule dicts.

    The UI column-mapper sends a compact array of
    ``{column, action, params?}`` objects.  Each is converted to a
    ``{"match": "column:<name>", "action": ..., "params": {...}}`` rule.
    Returns None when the param is absent (→ fall back to config_profile).
    """
    if not raw_param:
        return None
    try:
        items = json.loads(raw_param)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"Invalid 'rules' JSON: {exc}"
        ) from exc
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="'rules' must be a JSON array")

    rules: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        column = str(item.get("column", "")).strip()
        action = item.get("action")
        if not column or not action:
            continue
        rule: dict = {"match": f"column:{column}", "action": action}
        if isinstance(item.get("params"), dict):
            rule["params"] = item["params"]
        rules.append(rule)
    return rules


@router.post("/process/tabular/inspect")
@limiter.limit("30/minute")
async def inspect_tabular(request: Request):
    """Preview a tabular file's columns + sample values (no de-identification).

    Powers the UI column-mapper.  Query params: ``format``, ``delimiter``.
    Returns ``{format, row_count, columns: [{name, samples}]}``.
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    if not body:
        raise HTTPException(status_code=422, detail="Request body is empty")

    file_format = (request.query_params.get("format") or "csv").lower()
    delimiter = request.query_params.get("delimiter") or ","

    try:
        return await _service.inspect(body, file_format, delimiter)
    except NormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "inspect_tabular: unexpected error: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="Tabular inspect error") from exc


@router.post("/process/tabular")
@limiter.limit("30/minute")
async def process_tabular(request: Request):
    """De-identify a CSV / Excel / Parquet file by column rule.

    Query params:
      - ``format``: ``csv`` (default) | ``xlsx`` | ``parquet``
      - ``config_profile``: saved rule profile to apply (default ``auto``).
      - ``rules``: inline JSON array of ``{column, action, params?}`` (the UI
        column-mapper path); takes precedence over ``config_profile``.
      - ``delimiter``: CSV delimiter (default ``,``).
    """
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    if not body:
        raise HTTPException(status_code=422, detail="Request body is empty")

    file_format = (request.query_params.get("format") or "csv").lower()
    config_profile = request.query_params.get("config_profile") or "auto"
    delimiter = request.query_params.get("delimiter") or ","
    inline_rules = _parse_inline_rules(request.query_params.get("rules"))

    try:
        result = await _service.process_single(
            body, file_format, config_profile, delimiter, inline_rules
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
    except NormalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "process_tabular: unexpected error: %s", type(exc).__name__, exc_info=False
        )
        raise HTTPException(status_code=500, detail="Tabular processing error") from exc

    media = _MEDIA.get(file_format, "application/octet-stream")
    return Response(content=result, media_type=media)
