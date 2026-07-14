"""Internal DAG engine  runs workflow steps through the existing job system.

Design constraints (from the approved plan):
* The Postgres workflow store is the durable ledger; this engine is stateless
  logic over it.
* Steps are executed as ordinary ``Job``\\ s by the EXISTING worker  no new
  executor code. A step's job carries ``params["__workflow"] = {workflow_id,
  step_id}`` so the worker's terminal hook can route completion back here.
* Step status transitions go through the store's compare-and-set so a worker
  callback racing the reconciliation sweep cannot double-advance.

Failure policy (v1): a failed step marks all transitively-dependent steps
``SKIPPED`` and the workflow ``ERROR``; independent branches keep running.
"""

from __future__ import annotations

import logging
import uuid

from domain.jobs import Job, JobStatus
from domain.workflows import (
    StepStatus,
    Workflow,
    WorkflowStatus,
    WorkflowStep,
    validate_dag,
)
from pipeline.jobs.ports import JobStorePort

logger = logging.getLogger("medanon.workflows")

# Marker key injected into a step's job params so the worker can route the
# job's terminal status back to the owning workflow step.
WORKFLOW_PARAM_KEY = "__workflow"

# Job statuses that map to a successful / failed step outcome.
_JOB_SUCCESS = {JobStatus.DONE}
_JOB_FAILURE = {JobStatus.ERROR, JobStatus.DEAD, JobStatus.CANCELLED}


class WorkflowEngine:
    """Schedules and advances DAG workflows over a job store + workflow store."""

    def __init__(self, workflow_store, job_store: JobStorePort) -> None:
        self._wstore = workflow_store
        self._jobs = job_store

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(
        self, name: str, steps: list[WorkflowStep], params: dict | None = None
    ) -> Workflow:
        """Validate, persist, and enqueue the dependency-free steps."""
        validate_dag(steps)
        workflow = Workflow(
            id=str(uuid.uuid4()),
            name=name,
            steps=steps,
            status=WorkflowStatus.RUNNING,
            params=params or {},
        )
        self._wstore.create(workflow)
        # Enqueue every step whose dependencies are (trivially) satisfied.
        self._enqueue_ready_steps(workflow)
        return workflow

    # ------------------------------------------------------------------
    # Job → step callback (invoked by the worker terminal hook)
    # ------------------------------------------------------------------

    def on_job_terminal(self, job: Job) -> None:
        """Advance the owning workflow when a step's job reaches a terminal state.

        No-op for jobs that aren't part of a workflow. Safe to call for every
        job  the ``__workflow`` marker gates the work.
        """
        marker = (job.params or {}).get(WORKFLOW_PARAM_KEY)
        if not marker:
            return
        workflow_id = marker.get("workflow_id")
        step_id = marker.get("step_id")
        if not workflow_id or not step_id:
            return

        if job.status in _JOB_SUCCESS:
            self._on_step_done(workflow_id, step_id, job)
        elif job.status in _JOB_FAILURE:
            self._on_step_failed(workflow_id, step_id, job)
        # RUNNING/PENDING are not terminal  ignore.

    def _on_step_done(self, workflow_id: str, step_id: str, job: Job) -> None:
        changed = self._wstore.compare_and_set_step_status(
            workflow_id, step_id, StepStatus.RUNNING, StepStatus.DONE
        )
        if not changed:
            # Already advanced (duplicate callback / sweep race)  nothing to do.
            return
        workflow = self._wstore.get(workflow_id)
        if workflow is None:
            return
        self._enqueue_ready_steps(workflow)
        self._settle_workflow(workflow_id)

    def _on_step_failed(self, workflow_id: str, step_id: str, job: Job) -> None:
        new_status = (
            StepStatus.CANCELLED
            if job.status == JobStatus.CANCELLED
            else StepStatus.ERROR
        )
        changed = self._wstore.compare_and_set_step_status(
            workflow_id,
            step_id,
            StepStatus.RUNNING,
            new_status,
            error=job.error,
        )
        if not changed:
            return
        workflow = self._wstore.get(workflow_id)
        if workflow is None:
            return
        # Skip every step that transitively depends on the failed one.
        self._skip_descendants(workflow, step_id)
        # CAS RUNNING→ERROR so a concurrent cancel() (CANCELLED) is not clobbered.
        self._wstore.compare_and_set_workflow_status(
            workflow_id, WorkflowStatus.RUNNING, WorkflowStatus.ERROR
        )
        logger.warning(
            "workflow_step_failed workflow=%s step=%s -> workflow ERROR",
            workflow_id,
            step_id,
        )

    # ------------------------------------------------------------------
    # Scheduling helpers
    # ------------------------------------------------------------------

    def _enqueue_ready_steps(self, workflow: Workflow) -> None:
        """Enqueue every PENDING step whose dependencies are all DONE."""
        done = {s.id for s in workflow.steps if s.status == StepStatus.DONE}
        for step in workflow.steps:
            if step.status != StepStatus.PENDING:
                continue
            if not all(dep in done for dep in step.depends_on):
                continue
            self._enqueue_step(workflow, step)

    def _enqueue_step(self, workflow: Workflow, step: WorkflowStep) -> None:
        # CAS PENDING→READY first so two concurrent callbacks can't both enqueue.
        if not self._wstore.compare_and_set_step_status(
            workflow.id, step.id, StepStatus.PENDING, StepStatus.READY
        ):
            return
        params = dict(step.params)
        params[WORKFLOW_PARAM_KEY] = {
            "workflow_id": workflow.id,
            "step_id": step.id,
        }
        # Inject upstream results so a downstream step can consume them.
        upstream = self._collect_upstream_results(workflow, step)
        if upstream:
            params["upstream_results"] = upstream

        job = self._jobs.create(step.job_type, params)
        self._wstore.set_step_job(workflow.id, step.id, job.id)
        # READY→RUNNING: the worker will run it; status mirrors the job lifecycle.
        self._wstore.compare_and_set_step_status(
            workflow.id, step.id, StepStatus.READY, StepStatus.RUNNING
        )
        if hasattr(self._jobs, "notify_new_job"):
            try:
                self._jobs.notify_new_job(job.id)
            except Exception as exc:  # notification is best-effort
                logger.debug("notify_new_job_failed job=%s: %s", job.id, exc)
        logger.info(
            "workflow_step_enqueued workflow=%s step=%s job=%s type=%s",
            workflow.id,
            step.id,
            job.id,
            step.job_type,
        )

    def _collect_upstream_results(self, workflow: Workflow, step: WorkflowStep) -> dict:
        """Map ``{dep_step_id: result_path}`` for each completed dependency."""
        results: dict[str, str] = {}
        for dep_id in step.depends_on:
            dep = workflow.step(dep_id)
            if dep is None or dep.job_id is None:
                continue
            dep_job = self._jobs.get(dep.job_id)
            if dep_job is not None and dep_job.result_path:
                results[dep_id] = dep_job.result_path
        return results

    def _skip_descendants(self, workflow: Workflow, failed_step_id: str) -> None:
        """Mark all steps transitively depending on *failed_step_id* SKIPPED."""
        # Build adjacency: dep -> [steps depending on it].
        dependents: dict[str, list[str]] = {}
        for s in workflow.steps:
            for dep in s.depends_on:
                dependents.setdefault(dep, []).append(s.id)

        to_skip: set[str] = set()
        frontier = list(dependents.get(failed_step_id, []))
        while frontier:
            sid = frontier.pop()
            if sid in to_skip:
                continue
            to_skip.add(sid)
            frontier.extend(dependents.get(sid, []))

        for sid in to_skip:
            # Only skip steps not already terminal (PENDING/READY).
            for expected in (StepStatus.PENDING, StepStatus.READY):
                if self._wstore.compare_and_set_step_status(
                    workflow.id, sid, expected, StepStatus.SKIPPED
                ):
                    break

    def _settle_workflow(self, workflow_id: str) -> None:
        """Mark the workflow DONE when all steps are DONE (or ERROR otherwise)."""
        workflow = self._wstore.get(workflow_id)
        if workflow is None or workflow.status != WorkflowStatus.RUNNING:
            return
        statuses = {s.status for s in workflow.steps}
        # CAS RUNNING→terminal: if a concurrent cancel() already moved the
        # workflow out of RUNNING, the guard no-ops and CANCELLED stays sticky.
        if statuses <= {StepStatus.DONE}:
            if self._wstore.compare_and_set_workflow_status(
                workflow_id, WorkflowStatus.RUNNING, WorkflowStatus.DONE
            ):
                logger.info("workflow_done workflow=%s", workflow_id)
        elif statuses & {StepStatus.ERROR}:
            self._wstore.compare_and_set_workflow_status(
                workflow_id, WorkflowStatus.RUNNING, WorkflowStatus.ERROR
            )

    # ------------------------------------------------------------------
    # Cancellation + reconciliation
    # ------------------------------------------------------------------

    def cancel(self, workflow_id: str) -> bool:
        """Cancel a workflow: cancel in-flight step jobs, mark the rest cancelled."""
        workflow = self._wstore.get(workflow_id)
        if workflow is None:
            return False
        # Claim the terminal transition FIRST so a concurrent _settle_workflow
        # (driven by a final step completing at the same instant) sees a
        # non-RUNNING workflow and its RUNNING→DONE/ERROR CAS no-ops. Cover
        # both RUNNING and the not-yet-started PENDING state.
        claimed = self._wstore.compare_and_set_workflow_status(
            workflow_id, WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED
        ) or self._wstore.compare_and_set_workflow_status(
            workflow_id, WorkflowStatus.PENDING, WorkflowStatus.CANCELLED
        )
        if not claimed and workflow.status != WorkflowStatus.CANCELLED:
            # Already terminal (DONE/ERROR)  nothing to cancel.
            return False
        for step in workflow.steps:
            if step.status in (StepStatus.RUNNING, StepStatus.READY) and step.job_id:
                if hasattr(self._jobs, "cancel"):
                    try:
                        self._jobs.cancel(step.job_id)
                    except Exception as exc:
                        logger.debug(
                            "step_job_cancel_failed job=%s: %s", step.job_id, exc
                        )
            for expected in (
                StepStatus.PENDING,
                StepStatus.READY,
                StepStatus.RUNNING,
            ):
                if self._wstore.compare_and_set_step_status(
                    workflow_id, step.id, expected, StepStatus.CANCELLED
                ):
                    break
        return True

    def reconcile(self) -> None:
        """Re-derive readiness for non-terminal workflows (crash-window safety).

        If an enqueue was lost between the CAS to READY and job creation, the
        step would stall. This sweep re-checks readiness; it is idempotent
        thanks to the CAS guards in :meth:`_enqueue_step`.
        """
        try:
            workflows = self._wstore.list_non_terminal()
        except Exception as exc:
            logger.warning("workflow_reconcile_list_failed: %s", exc)
            return
        for workflow in workflows:
            try:
                # Re-enqueue any READY step that never got a job (lost enqueue).
                for step in workflow.steps:
                    if step.status == StepStatus.READY and step.job_id is None:
                        # Roll back to PENDING so _enqueue_ready_steps re-picks it.
                        self._wstore.compare_and_set_step_status(
                            workflow.id, step.id, StepStatus.READY, StepStatus.PENDING
                        )
                refreshed = self._wstore.get(workflow.id)
                if refreshed:
                    self._enqueue_ready_steps(refreshed)
                    self._settle_workflow(workflow.id)
            except Exception as exc:
                logger.warning(
                    "workflow_reconcile_failed workflow=%s: %s", workflow.id, exc
                )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_engine: WorkflowEngine | None = None


def init_workflow_engine(workflow_store, job_store: JobStorePort) -> WorkflowEngine:
    global _engine
    _engine = WorkflowEngine(workflow_store, job_store)
    return _engine


def get_workflow_engine() -> WorkflowEngine | None:
    return _engine
