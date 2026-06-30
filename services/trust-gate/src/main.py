"""Trust Gate microservice — pre-privacy data-quality / trust assessment.

Stateless HTTP service mirroring the scoring/analytics extraction pattern.
Exposes:
- ``POST /v1/trust/assess``       — assess a single resource / Bundle / list
- ``POST /v1/trust/assess/batch`` — assess a list of resources (one passport)
- ``GET  /health`` / ``/ready``   — probes
- ``GET  /metrics``               — Prometheus exposition

The anonymizer calls this service when ``TRUST_GATE_SERVICE_URL`` is set; on
failure it degrades to an advisory CONDITIONAL_PASS (never a silent PASS).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from starlette.responses import Response

from baseline import get_baseline_store
from cdm.fhir_to_omop import fhir_to_omop
from cdm.tabular_to_omop import tabular_to_omop
from connectors.file_connector import ConnectorError, parse_file
from connectors.sql_connector import SqlConnectorError, list_tables, query_table
from engine import assess, assess_omop
from label import build_label
from metric_catalog import (
    all_cards,
    resolve_critical_to_quality,
    resolve_use_case,
    use_cases,
)
from rules import PlausibilityRule, load_config, load_policy, validate_rules
from store import derive_findings, get_findings_store, get_passport_store
from terminology_client import get_terminology_client
from validator_client import get_validator_client

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
_log = logging.getLogger("trust_gate")

app = FastAPI(
    title="Trust Gate Service",
    version="1.0.0",
    description="Pre-privacy FHIR data-quality / trust assessment → Quality Passport.",
)

REQUESTS = Counter(
    "trust_gate_requests_total",
    "Number of assess requests by outcome",
    ["endpoint", "outcome"],
)
LATENCY = Histogram(
    "trust_gate_request_seconds", "Latency of assess requests", ["endpoint"]
)
DECISIONS = Counter(
    "trust_gate_decisions_total", "Passport decisions", ["decision"]
)

# Loaded once at import — the check config is small and static.
_RULES, _THRESHOLDS = load_config()
_POLICY = load_policy()
_log.info(
    "trust-gate loaded %d plausibility rules, %d threshold overrides, "
    "%d concordance rules, %d definitional bounds, %d resource thresholds",
    len(_RULES),
    len(_THRESHOLDS),
    len(_POLICY["concordance_rules"]),
    len(_POLICY["definitional_bounds"]),
    len(_POLICY.get("resource_thresholds", {})),
)


def _flatten(payload: Any) -> tuple[list[dict], list[str]]:
    """Normalise a single resource / Bundle / list into (resources, full_urls).

    Bundle ``entry.fullUrl`` values are preserved so the referential-integrity
    check can resolve intra-bundle ``urn:uuid:`` references.
    """
    resources: list[dict] = []
    full_urls: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            if node.get("resourceType") == "Bundle":
                for e in node.get("entry", []):
                    if isinstance(e, dict) and isinstance(e.get("resource"), dict):
                        resources.append(e["resource"])
                        fu = e.get("fullUrl")
                        if isinstance(fu, str) and fu:
                            full_urls.append(fu)
            else:
                resources.append(node)

    _walk(payload)
    return resources, full_urls


class AssessRequest(BaseModel):
    resource: dict[str, Any] | list[dict[str, Any]] = Field(...)
    dataset_id: str = "dataset"
    # Data provider identity — keys the retained assessment history.
    provider_id: str | None = None
    source_types: list[str] = Field(default_factory=lambda: ["fhir"])
    config_profile: str = "auto"
    provenance: dict[str, Any] = Field(default_factory=dict)
    # Selectable audit phases (None/empty → all). See phases.ALL_PHASES.
    phases: list[str] | None = None
    # Sector targets — each {id?, resource_types?, code_systems?} gets its own verdict.
    targets: list[dict[str, Any]] | None = None
    # Declared downstream use — makes the fitness verdict purpose-bound.
    intended_use: str | None = None
    # Declared use case — resolves to a metric subset via use_case_profiles.yaml.
    # Explicit `phases` (if given) still wins.
    use_case: str | None = None
    # Source-of-truth reference: {"records": {"Type/id": {field: expected}}}.
    reference: dict[str, Any] | None = None
    # Transparent-reporting attribution (Phase 4).
    lifecycle_stage: str = "operation"
    org_role: str = "data-receiving"
    # Opt-in Implementation-Guide conformance (e.g. "us_core"). None → IG check NA.
    ig: str | None = None
    # Optional idempotency key: a retry/replica race with the same key collapses
    # onto one persisted assessment row instead of duplicating it.
    idempotency_key: str | None = None
    # Caller-supplied custom expectations (same declarative schema as checks.yaml
    # plausibility rules). Merged into the rule set for this request only.
    custom_rules: list[dict[str, Any]] | None = None
    # Skip ONLY the slow external FHIR-validator calls (structural/profile/IG) while
    # keeping the in-process structural checks. Set false for high-volume scans.
    external_validation: bool = True


class BatchAssessRequest(BaseModel):
    resources: list[dict[str, Any]] = Field(..., max_length=50000)
    dataset_id: str = "dataset"
    provider_id: str | None = None
    # Bundle entry.fullUrl values, so referential integrity can resolve absolute /
    # urn:uuid references (NA without them). Carried separately because the batch
    # API takes a flat resource list, not a Bundle.
    full_urls: list[str] | None = None
    source_types: list[str] = Field(default_factory=lambda: ["fhir"])
    config_profile: str = "auto"
    provenance: dict[str, Any] = Field(default_factory=dict)
    phases: list[str] | None = None
    targets: list[dict[str, Any]] | None = None
    intended_use: str | None = None
    use_case: str | None = None
    reference: dict[str, Any] | None = None
    lifecycle_stage: str = "operation"
    org_role: str = "data-receiving"
    ig: str | None = None
    idempotency_key: str | None = None
    custom_rules: list[dict[str, Any]] | None = None
    external_validation: bool = True


class OmopAssessRequest(BaseModel):
    # Provider submits OMOP rows grouped by table, optionally with a column map...
    tables: dict[str, list[dict[str, Any]]] | None = None
    mapping: dict[str, dict[str, str]] | None = None
    # ...or FHIR resources, which are mapped to the OMOP core structurally.
    resources: list[dict[str, Any]] | None = None
    dataset_id: str = "dataset"
    provider_id: str | None = None
    source_types: list[str] = Field(default_factory=lambda: ["omop"])
    config_profile: str = "auto"
    provenance: dict[str, Any] = Field(default_factory=dict)
    intended_use: str | None = None
    use_case: str | None = None
    lifecycle_stage: str = "operation"
    org_role: str = "data-receiving"
    idempotency_key: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _persist(
    passport_dict: dict[str, Any],
    provider_id: str | None,
    idempotency_key: str | None = None,
) -> None:
    """Retain the passport + derive open remediation findings. Best-effort: a
    store outage must never block or fail an assessment (PDSA loop, Phase 5).

    A supplied ``idempotency_key`` makes the write idempotent (an at-least-once
    retry or a racing replica collapses onto one assessment row)."""
    store = get_passport_store()
    if store is None:
        return
    try:
        assessment_id = store.save(
            passport_dict, provider_id=provider_id, idempotency_key=idempotency_key
        )
    except Exception:  # noqa: BLE001 — persistence is non-fatal
        _log.warning("passport persistence failed (non-fatal)", exc_info=True)
        return
    fstore = get_findings_store()
    if fstore is None:
        return
    try:
        for finding in derive_findings(passport_dict, assessment_id):
            fstore.create(finding)
    except Exception:  # noqa: BLE001 — finding derivation is non-fatal
        _log.warning("findings derivation failed (non-fatal)", exc_info=True)


def _parse_custom_rules(raw: list[dict] | None) -> list[PlausibilityRule]:
    """Parse caller-supplied custom expectations (same declarative schema as
    config/checks.yaml) into rules. Invalid rules raise 422 — a custom expectation
    that does not parse must not be silently dropped. Empty/None → no extra rules."""
    if not raw:
        return []
    errors = validate_rules(raw)
    if errors:
        raise HTTPException(status_code=422, detail={"custom_rules": errors})
    return [PlausibilityRule.model_validate(r) for r in raw]


def _assess(resources: list[dict], req, full_urls: list[str] | None = None) -> dict[str, Any]:
    # Use-case selection (Phase 2): resolve the declared use case to a phase
    # subset + threshold tweaks. An explicit `phases` request still wins; an
    # unknown use case falls back to all phases.
    use_case = getattr(req, "use_case", None)
    uc_phases, uc_thresholds = resolve_use_case(use_case)
    phases = getattr(req, "phases", None) or uc_phases
    thresholds = {**_THRESHOLDS, **uc_thresholds} if uc_thresholds else _THRESHOLDS
    critical_check_ids = resolve_critical_to_quality(use_case)
    rules = _RULES + _parse_custom_rules(getattr(req, "custom_rules", None))

    passport = assess(
        resources,
        dataset_id=req.dataset_id,
        source_types=req.source_types,
        config_profile=req.config_profile,
        provenance=req.provenance,
        plausibility_rules=rules,
        threshold_overrides=thresholds,
        validator=get_validator_client(),
        terminology_client=get_terminology_client(),
        baseline_store=get_baseline_store(),
        definitional_bounds=_POLICY["definitional_bounds"],
        concordance_rules=_POLICY["concordance_rules"],
        resource_thresholds=_POLICY.get("resource_thresholds"),
        phases=phases,
        full_urls=full_urls,
        targets=getattr(req, "targets", None),
        intended_use=getattr(req, "intended_use", None),
        reference=getattr(req, "reference", None),
        lifecycle_stage=getattr(req, "lifecycle_stage", "operation"),
        org_role=getattr(req, "org_role", "data-receiving"),
        critical_check_ids=critical_check_ids,
        ig=getattr(req, "ig", None),
        external_validation=getattr(req, "external_validation", True),
    )
    DECISIONS.labels(decision=passport.decision).inc()
    result = passport.to_dict()
    if use_case:
        result.get("evaluation", {})["use_case"] = use_case
    result["label"] = build_label(result)
    _persist(
        result,
        getattr(req, "provider_id", None),
        getattr(req, "idempotency_key", None),
    )
    return result


def _require_store():
    store = get_passport_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="no passport store configured (set TRUST_GATE_STORE_DB_URL or TRUST_GATE_STORE_DB)",
        )
    return store


@app.get("/v1/providers/{provider_id}/assessments")
def provider_assessments(provider_id: str, limit: int = 50) -> dict[str, Any]:
    store = _require_store()
    return {
        "provider_id": provider_id,
        "assessments": store.list_assessments(provider_id, limit),
    }


@app.get("/v1/datasets/{dataset_id}/history")
def dataset_history(dataset_id: str, limit: int = 50) -> dict[str, Any]:
    store = _require_store()
    return {"dataset_id": dataset_id, "history": store.history(dataset_id, limit)}


@app.get("/v1/datasets/{dataset_id}/trend")
def dataset_trend(dataset_id: str, limit: int = 100) -> dict[str, Any]:
    store = _require_store()
    return {"dataset_id": dataset_id, "trend": store.trend(dataset_id, limit)}


@app.get("/v1/datasets/{dataset_id}/audit")
def dataset_audit(dataset_id: str, limit: int = 100) -> dict[str, Any]:
    """Append-only ALCOA++ assessment audit trail for a dataset (Phase 6.3)."""
    store = _require_store()
    return {"dataset_id": dataset_id, "audit": store.audit(dataset_id, limit)}


@app.get("/v1/assessments/{assessment_id}")
def get_assessment(assessment_id: str) -> dict[str, Any]:
    store = _require_store()
    passport = store.get(assessment_id)
    if passport is None:
        raise HTTPException(status_code=404, detail="assessment not found")
    return passport


@app.get("/v1/datasets/{dataset_id}/gdpr-record")
def dataset_gdpr_record(
    dataset_id: str, assessment_id: str | None = None
) -> dict[str, Any]:
    """GDPR Art. 30 record of processing activities for the read-only QC evaluation
    (EU compliance): the latest passport's provenance + reproducibility + ALCOA++
    trail, assembled into an EU-audit-ready record. Pass ``assessment_id`` to pin
    a specific assessment."""
    from eu_audit import build_processing_record

    store = _require_store()
    if assessment_id is None:
        # history() returns summary rows; resolve the latest id to the full passport
        # (the summary omits the evaluation/reproducibility block the record needs).
        hist = store.history(dataset_id, limit=1)
        assessment_id = hist[0].get("id") if hist else None
    passport = store.get(assessment_id) if assessment_id else None
    if passport is None:
        raise HTTPException(status_code=404, detail="no assessment found for dataset")
    return build_processing_record(passport, store.audit(dataset_id))


class FindingCreate(BaseModel):
    dataset_id: str
    check_id: str = ""
    severity: str = "major"
    assessment_id: str | None = None
    note: str = ""
    owner: str | None = None


class FindingTransition(BaseModel):
    status: str | None = None
    root_cause: str | None = None
    owner: str | None = None
    note: str | None = None


def _require_findings():
    store = get_findings_store()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="no findings store configured (set TRUST_GATE_STORE_DB_URL or TRUST_GATE_STORE_DB)",
        )
    return store


@app.get("/v1/findings")
def list_findings(
    dataset_id: str | None = None, status: str | None = None, limit: int = 100
) -> dict[str, Any]:
    store = _require_findings()
    return {"findings": store.list(dataset_id=dataset_id, status=status, limit=limit)}


@app.post("/v1/findings")
def create_finding(req: FindingCreate) -> dict[str, Any]:
    store = _require_findings()
    fid = store.create(req.model_dump())
    return store.get(fid)


@app.get("/v1/findings/{finding_id}")
def get_finding(finding_id: str) -> dict[str, Any]:
    store = _require_findings()
    finding = store.get(finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return finding


@app.post("/v1/findings/{finding_id}/transition")
def transition_finding(finding_id: str, req: FindingTransition) -> dict[str, Any]:
    store = _require_findings()
    if req.status is not None and req.status not in ("open", "triaged", "resolved"):
        raise HTTPException(status_code=400, detail="invalid status")
    updated = store.transition(finding_id, **req.model_dump())
    if updated is None:
        raise HTTPException(status_code=404, detail="finding not found")
    return updated


@app.get("/v1/datasets/{dataset_id}/findings")
def dataset_findings(dataset_id: str, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    store = _require_findings()
    return {
        "dataset_id": dataset_id,
        "findings": store.list(dataset_id=dataset_id, status=status, limit=limit),
    }


@app.get("/v1/metric-catalog")
def metric_catalog() -> dict[str, Any]:
    """The documented metric cards (Phase 2): one per built-in check."""
    return {"cards": all_cards()}


@app.get("/v1/use-cases")
def use_case_profiles() -> dict[str, Any]:
    """The use-case decision-tree: declared use → metric subset (phases)."""
    return {"use_cases": use_cases()}


@app.post("/v1/trust/assess")
def assess_endpoint(req: AssessRequest) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        resources, full_urls = _flatten(req.resource)
        result = _assess(resources, req, full_urls=full_urls)
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


@app.post("/v1/trust/assess/omop")
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
        _persist(result, req.provider_id, getattr(req, "idempotency_key", None))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="assess_omop", outcome="error").inc()
        _log.exception("assess_omop failed")
        raise HTTPException(status_code=500, detail=f"assess failed: {exc}") from exc
    REQUESTS.labels(endpoint="assess_omop", outcome="ok").inc()
    LATENCY.labels(endpoint="assess_omop").observe(time.monotonic() - t0)
    return result


@app.post("/v1/trust/assess/batch")
def assess_batch_endpoint(req: BatchAssessRequest) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        result = _assess(
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


# ---------------------------------------------------------------------------
# Connector endpoints
# ---------------------------------------------------------------------------

class SqlConnectRequest(BaseModel):
    driver: str = Field(..., description="postgresql | mysql | sqlite")
    host: str = Field("", description="Database host (empty for SQLite)")
    port: int | None = Field(None, description="Database port (uses driver default if omitted)")
    database: str = Field(..., description="Database name or SQLite file path")
    username: str = Field("", description="Database username")
    password: str = Field("", description="Database password")
    query: str = Field(..., description="SELECT query to run (DDL/DML rejected)")
    table_name: str | None = Field(None, description="Key for the result table (defaults to 'query_result')")
    mapping: dict[str, dict[str, str]] | None = Field(None, description="OMOP column mapping: {table: {omop_col: source_col}}")
    dataset_id: str | None = None
    provider_id: str | None = None
    config_profile: str | None = None
    provenance: str | None = None
    intended_use: str | None = None
    lifecycle_stage: str | None = None
    org_role: str | None = None
    use_case: str | None = None


class SqlTablesRequest(BaseModel):
    driver: str
    host: str = ""
    port: int | None = None
    database: str
    username: str = ""
    password: str = ""


class FileAssessQuery(BaseModel):
    dataset_id: str | None = None
    provider_id: str | None = None
    table_name: str | None = None
    sheet: str | None = None
    mapping: str | None = Field(None, description="JSON-encoded mapping: {table: {omop_col: src_col}}")
    config_profile: str | None = None
    provenance: str | None = None
    intended_use: str | None = None
    use_case: str | None = None


@app.post("/v1/trust/connectors/file")
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

    The file format is auto-detected from the filename extension.
    Excel files with multiple sheets are assessed as separate tables.
    Column mapping (JSON string) remaps source columns to OMOP column names.
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
                raise HTTPException(status_code=400, detail=f"Invalid mapping JSON: {exc}") from exc

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

            result = _assess(resources, _Req(), full_urls=None)  # type: ignore[arg-type]
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
            _persist(result, provider_id)

        result.setdefault("connector", {"source": "file", "filename": filename, "format": fmt})

    except ConnectorError as exc:
        REQUESTS.labels(endpoint="connectors_file", outcome="error").inc()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        REQUESTS.labels(endpoint="connectors_file", outcome="error").inc()
        raise
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="connectors_file", outcome="error").inc()
        _log.exception("file connector failed")
        raise HTTPException(status_code=500, detail=f"File assessment failed: {exc}") from exc

    REQUESTS.labels(endpoint="connectors_file", outcome="ok").inc()
    LATENCY.labels(endpoint="connectors_file").observe(time.monotonic() - t0)
    return result


@app.post("/v1/trust/connectors/sql")
def assess_sql_endpoint(req: SqlConnectRequest) -> dict[str, Any]:
    """Assess data quality by running a SELECT query against a SQL database.

    The rows are normalised to OMOP CDM (with optional column mapping) and
    assessed with the full OHDSI-DQD check suite.

    Requires TRUST_GATE_SQL_ALLOWED_HOSTS to contain the target host.
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
        _persist(result, req.provider_id, getattr(req, "idempotency_key", None))
    except SqlConnectorError as exc:
        REQUESTS.labels(endpoint="connectors_sql", outcome="error").inc()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        REQUESTS.labels(endpoint="connectors_sql", outcome="error").inc()
        _log.exception("sql connector failed")
        raise HTTPException(status_code=500, detail=f"SQL assessment failed: {exc}") from exc

    REQUESTS.labels(endpoint="connectors_sql", outcome="ok").inc()
    LATENCY.labels(endpoint="connectors_sql").observe(time.monotonic() - t0)
    return result


@app.post("/v1/trust/connectors/sql/tables")
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
        raise HTTPException(status_code=500, detail=f"Connection failed: {exc}") from exc
    return {"tables": tables, "driver": req.driver, "database": req.database}
