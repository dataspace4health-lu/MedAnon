"""Connector endpoints: assess data quality from an uploaded file or a SQL query."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile

from cdm.tabular_to_omop import tabular_to_omop
from connectors.file_connector import ConnectorError, parse_file
from connectors.sql_connector import SqlConnectorError, list_tables, query_table
from engine import assess_omop
from label import build_label
from metric_catalog import resolve_critical_to_quality

from api.metrics import DECISIONS, LATENCY, REQUESTS
from api.schemas import SqlConnectRequest, SqlTablesRequest
from api.service import persist, run_assessment

_log = logging.getLogger("trust_gate")
router = APIRouter()


@router.post("/v1/trust/connectors/file")
async def assess_file_endpoint(
    file: UploadFile = File(...),
    dataset_id: str | None = None,
    provider_id: str | None = None,
    table_name: str | None = None,
    sheet: str | None = None,
    mapping: str | None = None,
    config_profile: str | None = None,
    provenance: str | None = None,
    intended_use: str | None = None,
    use_case: str | None = None,
) -> dict[str, Any]:
    """Assess data quality from an uploaded file (CSV, TSV, Excel, JSON, NDJSON).

    The file format is auto-detected from the filename extension. Excel files with
    multiple sheets are assessed as separate tables. Column mapping (JSON string)
    remaps source columns to OMOP column names.
    """
    t0 = time.monotonic()
    filename = file.filename or "upload"
    try:
        content = await file.read()
        fmt, payload = parse_file(content, filename, sheet=sheet, table_name=table_name)

        parsed_mapping: dict[str, dict[str, str]] | None = None
        if mapping:
            import json as _json

            try:
                parsed_mapping = _json.loads(mapping)
            except Exception as exc:
                raise HTTPException(
                    status_code=400, detail=f"Invalid mapping JSON: {exc}"
                ) from exc

        if fmt == "fhir":
            resources = payload  # type: ignore[assignment]

            class _Req:
                phases = None
                targets = None
                intended_use = intended_use
                config_profile = config_profile
                provenance = provenance
                lifecycle_stage = None
                org_role = None
                use_case = use_case

            result = run_assessment(resources, _Req(), full_urls=None)  # type: ignore[arg-type]
        else:
            tables = payload  # type: ignore[assignment]
            omop = tabular_to_omop(tables, parsed_mapping)
            passport = assess_omop(
                omop,
                dataset_id=dataset_id,
                source_types=["file", fmt],
                config_profile=config_profile,
                provenance=provenance or f"file:{filename}",
                intended_use=intended_use,
                critical_check_ids=resolve_critical_to_quality(use_case),
            )
            DECISIONS.labels(decision=passport.decision).inc()
            result = passport.to_dict()
            result["label"] = build_label(result)
            persist(result, provider_id)

        result.setdefault(
            "connector", {"source": "file", "filename": filename, "format": fmt}
        )

    except ConnectorError as exc:
        REQUESTS.labels(endpoint="connectors_file", outcome="error").inc()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        REQUESTS.labels(endpoint="connectors_file", outcome="error").inc()
        raise
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="connectors_file", outcome="error").inc()
        _log.exception("file connector failed")
        raise HTTPException(
            status_code=500, detail=f"File assessment failed: {exc}"
        ) from exc

    REQUESTS.labels(endpoint="connectors_file", outcome="ok").inc()
    LATENCY.labels(endpoint="connectors_file").observe(time.monotonic() - t0)
    return result


@router.post("/v1/trust/connectors/sql")
def assess_sql_endpoint(req: SqlConnectRequest) -> dict[str, Any]:
    """Assess data quality by running a SELECT query against a SQL database.

    The rows are normalised to OMOP CDM (with optional column mapping) and assessed
    with the full OHDSI-DQD check suite. Requires TRUST_GATE_SQL_ALLOWED_HOSTS to
    contain the target host.
    """
    t0 = time.monotonic()
    try:
        _fmt, payload = query_table(
            driver=req.driver,
            host=req.host,
            port=req.port,
            database=req.database,
            username=req.username,
            password=req.password,
            query=req.query,
            table_name=req.table_name,
        )
        omop = tabular_to_omop(payload, req.mapping)
        passport = assess_omop(
            omop,
            dataset_id=req.dataset_id,
            source_types=["sql", req.driver],
            config_profile=req.config_profile,
            provenance=req.provenance or f"sql:{req.driver}:{req.database}",
            intended_use=req.intended_use,
            lifecycle_stage=req.lifecycle_stage,
            org_role=req.org_role,
            critical_check_ids=resolve_critical_to_quality(req.use_case),
        )
        DECISIONS.labels(decision=passport.decision).inc()
        result = passport.to_dict()
        result["label"] = build_label(result)
        result["connector"] = {
            "source": "sql",
            "driver": req.driver,
            "database": req.database,
            "table_name": req.table_name or "query_result",
        }
        persist(result, req.provider_id, getattr(req, "idempotency_key", None))
    except SqlConnectorError as exc:
        REQUESTS.labels(endpoint="connectors_sql", outcome="error").inc()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="connectors_sql", outcome="error").inc()
        _log.exception("sql connector failed")
        raise HTTPException(
            status_code=500, detail=f"SQL assessment failed: {exc}"
        ) from exc

    REQUESTS.labels(endpoint="connectors_sql", outcome="ok").inc()
    LATENCY.labels(endpoint="connectors_sql").observe(time.monotonic() - t0)
    return result


@router.post("/v1/trust/connectors/sql/tables")
def sql_list_tables_endpoint(req: SqlTablesRequest) -> dict[str, Any]:
    """Test the SQL connection and return the visible table names."""
    try:
        tables = list_tables(
            driver=req.driver,
            host=req.host,
            port=req.port,
            database=req.database,
            username=req.username,
            password=req.password,
        )
    except SqlConnectorError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"Connection failed: {exc}"
        ) from exc
    return {"tables": tables, "driver": req.driver, "database": req.database}
