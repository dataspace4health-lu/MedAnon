"""Canonical workflow templates.

Express the common bulk operations as small DAGs so callers can run them via
``POST /v1/workflows`` without changing the existing ``POST /v1/jobs/*`` API
(which stays as-is). These are plain builders returning ``list[WorkflowStep]``
 the engine and either orchestration backend consume them unchanged.
"""

from __future__ import annotations

from domain.workflows import WorkflowStep


def bulk_export_workflow(params: dict) -> list[WorkflowStep]:
    """fetch+deid → (optional) upload → (optional) score.

    ``params`` mirrors the bulk-export job request. Keys consumed here:
      * ``upload`` (bool): add an upload step targeting the FHIR target server.
      * ``score`` (bool): add a terminal scoring/summary step.
    The ``fetch_deid`` step runs the existing ``bulk-export`` executor with
    upload disabled; the optional ``upload`` step runs ``bulk-import`` over the
    upstream result.
    """
    deid_params = {k: v for k, v in params.items() if k not in ("upload", "score")}
    deid_params["upload"] = False
    steps = [
        WorkflowStep(id="fetch_deid", job_type="bulk-export", params=deid_params),
    ]
    last = "fetch_deid"
    if params.get("upload"):
        steps.append(
            WorkflowStep(
                id="upload",
                job_type="bulk-import",
                params={"target": params.get("target", {})},
                depends_on=[last],
            )
        )
        last = "upload"
    if params.get("score"):
        steps.append(
            WorkflowStep(
                id="score",
                job_type="reprocess",
                params={"mode": "score-only"},
                depends_on=[last],
            )
        )
    return steps


def cohort_workflow(params: dict) -> list[WorkflowStep]:
    """Cohort fetch+deid → (optional) upload, mirroring bulk_export_workflow."""
    deid_params = {k: v for k, v in params.items() if k not in ("upload", "score")}
    deid_params["upload"] = False
    steps = [
        WorkflowStep(id="fetch_deid", job_type="cohort", params=deid_params),
    ]
    if params.get("upload"):
        steps.append(
            WorkflowStep(
                id="upload",
                job_type="bulk-import",
                params={"target": params.get("target", {})},
                depends_on=["fetch_deid"],
            )
        )
    return steps


# name → builder, for the API to expose named templates.
TEMPLATES = {
    "bulk-export": bulk_export_workflow,
    "cohort": cohort_workflow,
}
