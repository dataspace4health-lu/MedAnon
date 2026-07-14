"""Trust Gate microservice — pre-privacy data-quality / trust assessment.

Composition root: builds the FastAPI ``app`` and mounts the routers from the ``api``
package. The service assesses FHIR (or OMOP CDM) data quality and returns a graded
"Quality Passport". The anonymizer calls it when ``TRUST_GATE_SERVICE_URL`` is set;
on failure it degrades to an advisory CONDITIONAL_PASS (never a silent PASS).

Surfaces (see api/routers/): /v1/trust/assess[/omop|/batch], the file/SQL connectors,
retained-assessment reads, remediation findings, the metric catalog, and probes.
The container runs ``uvicorn main:app``.
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

from fastapi import FastAPI  # noqa: E402
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: E402
from starlette.responses import Response  # noqa: E402

import api.config  # noqa: E402,F401  — import triggers the one-time config load + log
from api.routers import (  # noqa: E402
    assess,
    catalog,
    connectors,
    datasets,
    findings,
)

app = FastAPI(
    title="Trust Gate Service",
    version="1.0.0",
    description="Pre-privacy FHIR data-quality / trust assessment → Quality Passport.",
)

for _router in (
    assess.router,
    connectors.router,
    datasets.router,
    findings.router,
    catalog.router,
):
    app.include_router(_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Backwards-compatible surface: tests (and any in-process caller) that import the
# request models, service helpers, or endpoint functions from ``main`` keep working.
# The real code lives in the ``api`` package; these are the module's public re-exports.
# ---------------------------------------------------------------------------
from api.config import RULES as _RULES, THRESHOLDS as _THRESHOLDS  # noqa: E402,F401
from api.routers.assess import (  # noqa: E402,F401
    assess_batch_endpoint,
    assess_endpoint,
    assess_omop_endpoint,
)
from api.routers.datasets import (  # noqa: E402,F401
    dataset_audit,
    dataset_gdpr_record,
    dataset_history,
    dataset_trend,
    get_assessment,
    provider_assessments,
)
from api.routers.findings import (  # noqa: E402,F401
    create_finding,
    dataset_findings,
    get_finding,
    list_findings,
    transition_finding,
)
from api.schemas import (  # noqa: E402,F401
    AssessRequest,
    BatchAssessRequest,
    FindingCreate,
    FindingTransition,
    OmopAssessRequest,
)
from api.service import flatten as _flatten, run_assessment as _assess  # noqa: E402,F401
