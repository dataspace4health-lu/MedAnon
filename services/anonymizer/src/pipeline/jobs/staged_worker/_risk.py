"""staged_worker._risk — risk-driven k-anonymity export executor.

The risk-driven export is a three-phase staged job:

  1. **fetching**  — pull every resource from the source FHIR server and stage
     its *reference* (resource_id, resource_type, fhir_source_url) in the
     staging table.  No patient data is persisted (same contract as every
     other staged executor — see ``_core._fetch_staged_resources``).
  2. **solving**   — re-fetch the staged resources, build a quasi-identifier
     index (:func:`pipeline.privacy.qi_index.build_qi_index`), and search the
     generalisation lattice (:func:`pipeline.privacy.lattice.solve`) for the
     least-information-loss level vector that achieves the target k (and
     optional l-diversity) within the suppression cap.  The resulting
     :class:`~pipeline.privacy.lattice.GeneralizationPlan` is stored in the
     checkpoint so a crash-resume can re-apply it without re-solving.
  3. **applying**  — apply the plan: suppress small-class Patients (and their
     linked resources when ``suppress_linked`` is set), generalise the QI
     fields on the survivors, de-identify the survivors through the normal
     :func:`pipeline.processor.process_data_batch` pipeline, and write the
     result to ``{MEDANON_OUTPUT_DIR}/{job_id}.ndjson``.

Unlike the bulk-export / cohort executors this job needs the *whole* cohort in
memory at once (the lattice solver is global), so it does not stream — it
re-fetches all staged resources, solves, applies, and writes in one pass.  The
QI index uses reservoir sampling (``MEDANON_KANON_MAX_PATIENTS``) so the
solver stays bounded even on large cohorts; the apply pass streams the full
cohort back through the pipeline.

The public entry point is re-exported via ``staged_worker.__init__``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from domain.jobs import JobStatus
from pipeline.jobs.checkpoint import load_checkpoint, save_checkpoint
from integrations.storage import store_result

_log = logging.getLogger("medanon.staged_worker")

# Module-level so tests can patch ``staged_worker._risk._OUTPUT_DIR`` and the
# checkpoint helpers without touching the shared ``_core`` constants.
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _refetch_all_staged(staging, job_id, fhir_base_url, token, timeout):
    """Re-fetch every staged resource for *job_id* from the source FHIR server.

    Staging rows hold only references; this resolves them back to full FHIR
    resources so the lattice solver and the apply pass can operate on real
    data.  Resources the server no longer returns (deleted, not found) are
    silently omitted.

    Returns a list of resource dicts.  Reuses ``_core._fetch_staged_resources``
    so reference resolution and per-type batch grouping stay in one place.
    """
    from pipeline.jobs.staged_worker._core import _fetch_staged_resources

    rows = list(staging.get_all_resources(job_id))
    if not rows:
        return []
    return _fetch_staged_resources(rows)


def _cancelled(store, job) -> bool:
    fresh = store.get(job.id)
    return bool(fresh and fresh.status == JobStatus.CANCELLED)


# ---------------------------------------------------------------------------
# Public executor
# ---------------------------------------------------------------------------


def execute_risk_driven_export_staged(job, store, staging) -> None:
    """Staged risk-driven k-anonymity export (synchronous — runs via to_thread).

    Phases (``checkpoint['phase']``): ``fetching`` → ``solving`` → ``done``.
    Crash-resume re-enters at the recorded phase.  The chosen
    ``generalization_plan`` is persisted in the checkpoint so the ``solving``
    work is never repeated.

    Raises ``RuntimeError`` if *staging* is None — risk-driven export cannot
    operate without a staging store (it needs the full cohort to solve the
    lattice).
    """
    if staging is None:
        raise RuntimeError(
            "risk-driven export requires a staging store "
            "(MEDANON_STAGING_DB_URL / MEDANON_APP_DB_URL must be configured)"
        )

    from pipeline.config.service import get_settings
    from pipeline.processor import _get_default_pseudonymizer, process_data_batch
    from pipeline.privacy.qi_index import build_qi_index
    from pipeline.privacy.lattice import solve, GeneralizationPlan
    from pipeline.privacy.apply import filter_and_apply

    params = job.params
    server_url = params.get("server_url", "")
    token = params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = float(params.get("timeout", 30))
    profile = params.get("config_profile", "auto")

    # privacy_model may be supplied inline on the job params or carried on the
    # resolved Settings.  The inline form takes precedence so an API caller can
    # override the profile's model per-request.
    settings = get_settings(profile)
    privacy_model = params.get("privacy_model") or getattr(
        settings, "privacy_model", None
    )
    if not privacy_model:
        raise RuntimeError(
            "risk-driven export requires a privacy_model "
            "(inline on the job or via the config profile)"
        )

    pseudonymizer = _get_default_pseudonymizer()

    Path(_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    output_path = os.path.join(_OUTPUT_DIR, f"{job.id}.ndjson")

    checkpoint = load_checkpoint(job) or {}
    phase = checkpoint.get("phase", "fetching")

    # ── Phase 1: fetching ────────────────────────────────────────────────
    # Stage every resource type from the source server.  Mirrors the bulk
    # export fetch but without per-type streaming output — risk-driven needs
    # the whole cohort, so we stage refs then re-fetch in the solve phase.
    if phase == "fetching":
        from integrations.fhir.client import (
            fetch_resource_type,
            get_capability_statement,
        )
        from pipeline.jobs.staged_worker._core import _INFRA

        save_checkpoint(store, job, {"phase": "fetching", "staged_count": 0})
        staged_count = 0
        resource_types = params.get("resource_types")
        if not resource_types:
            cap = get_capability_statement(server_url, token=token, timeout=timeout)
            resource_types = [rt for rt in cap if rt not in _INFRA] if cap else []

        for rt in resource_types:
            if _cancelled(store, job):
                _log.info("risk_driven_cancelled job=%s phase=fetching", job.id)
                return
            page_url = None
            while True:
                page, page_url = fetch_resource_type(
                    server_url, rt, token=token, timeout=timeout, start_url=page_url
                )
                if page:
                    staged_count += staging.stage_batch(
                        job.id, page, fhir_source_url=server_url
                    )
                if not page_url:
                    break

        phase = "solving"
        save_checkpoint(store, job, {"phase": "solving", "staged_count": staged_count})

    # ── Phase 2: solving ─────────────────────────────────────────────────
    # Re-fetch the cohort, build the QI index, solve the lattice.  The plan is
    # cached in the checkpoint so a crash between solve and apply does not
    # re-run the (potentially expensive) lattice search.
    resources = _refetch_all_staged(staging, job.id, server_url, token, timeout)

    plan_dict = checkpoint.get("generalization_plan")
    if phase == "solving" and not plan_dict:
        if _cancelled(store, job):
            _log.info("risk_driven_cancelled job=%s phase=solving", job.id)
            return

        qi_index = build_qi_index(resources, privacy_model)
        plan = solve(qi_index, privacy_model)

        if not plan.feasible and plan.on_unsatisfiable == "fail":
            raise RuntimeError(
                f"risk-driven export infeasible: could not achieve target_k="
                f"{privacy_model.get('target_k')} within suppression cap "
                f"{privacy_model.get('max_suppression')} "
                f"(achieved_k={plan.achieved_k}, "
                f"suppression_rate={plan.suppression_rate:.3f})"
            )

        plan_dict = {
            "achieved_k": plan.achieved_k,
            "achieved_l": plan.achieved_l,
            "levels": dict(plan.levels),
            "node": list(plan.node),
            "suppressed_ids": sorted(plan.suppressed_ids),
            "suppressed_count": plan.suppressed_count,
            "suppression_rate": plan.suppression_rate,
            "information_loss": plan.information_loss,
            "feasible": plan.feasible,
        }
        save_checkpoint(
            store,
            job,
            {
                "phase": "solving",
                "staged_count": checkpoint.get("staged_count", len(resources)),
                "generalization_plan": plan_dict,
            },
        )
    else:
        # Resume path: rebuild a GeneralizationPlan from the cached dict.
        plan = GeneralizationPlan(
            levels=dict(plan_dict.get("levels", {})),
            node=tuple(plan_dict.get("node", ())),
            suppressed_ids=set(plan_dict.get("suppressed_ids", [])),
            achieved_k=plan_dict.get("achieved_k", 0),
            achieved_l=plan_dict.get("achieved_l"),
            suppressed_count=plan_dict.get("suppressed_count", 0),
            suppression_rate=plan_dict.get("suppression_rate", 0.0),
            information_loss=plan_dict.get("information_loss", 0.0),
            feasible=plan_dict.get("feasible", False),
        )

    # ── Phase 3: applying ────────────────────────────────────────────────
    # Suppress + generalise the cohort, then de-identify the survivors through
    # the normal pipeline and write NDJSON.  Suppression happens BEFORE the
    # de-id pass so suppressed patients (and their linked resources) never
    # reach gPAS/NLP — no wasted work and no risk of a suppressed value leaking.
    survivors = filter_and_apply(resources, plan, privacy_model)

    written = 0
    with open(output_path, "w", encoding="utf-8") as fh:
        from utils.json_fast import dumps as _json_dumps

        # De-identify in batches so a single large cohort does not balloon RAM.
        from pipeline.jobs.staged_worker._core import _BATCH_SIZE

        for start in range(0, len(survivors), _BATCH_SIZE):
            if _cancelled(store, job):
                _log.info("risk_driven_cancelled job=%s phase=applying", job.id)
                return
            batch = survivors[start : start + _BATCH_SIZE]
            processed = process_data_batch(
                batch, settings, pseudonymizer, attach_manifest=True
            )
            for result in processed:
                fh.write(_json_dumps(result) + "\n")
                written += 1

    job.result_path = store_result(job.id, output_path)

    final_plan = dict(plan_dict)
    save_checkpoint(
        store,
        job,
        {
            "phase": "done",
            "staged_count": checkpoint.get("staged_count", len(resources)),
            "processed": written,
            "generalization_plan": final_plan,
        },
    )
    _log.info(
        "risk_driven_done job=%s written=%d achieved_k=%s suppressed=%d rate=%.3f",
        job.id,
        written,
        final_plan.get("achieved_k"),
        len(final_plan.get("suppressed_ids", [])),
        final_plan.get("suppression_rate", 0.0),
    )
