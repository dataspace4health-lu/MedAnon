"""FHIR de-identification / pseudonymization orchestrator.

Public surface:

- :func:`process_data` — single resource / Bundle / list (signature unchanged).
- :func:`process_data_batch` — batch of resources with cross-resource gPAS
  batching (one HTTP call for up to ``MEDANON_BATCH_SIZE`` resources).

Pipeline stages per batch:

1. **match**       — FHIRPath evaluation + action dispatch (parallel, per resource)
2. **phi_detection** — NLP batch detection + replacement across all resources
   **pseudonymize** — gPAS batch lookup across all resources
   (stages 2 & 3 run **concurrently** — NLP scrubs free-text fields while gPAS
   fetches pseudonyms for structured identifiers; they touch disjoint paths)
3. **finalize**    — gPAS write-back + post-processing per resource (parallel)

All logic is delegated to focused sub-modules:

- :mod:`pipeline.rule_matcher`      — FHIRPath evaluation + rule index
- :mod:`pipeline.action_dispatcher` — match stage: action dispatch + work accumulation
- :mod:`pipeline.nlp_orchestrator`  — phi_detection stage: batch NLP
- :mod:`pipeline.gpas_orchestrator` — pseudonymize stage: batch gPAS call
- :mod:`pipeline.post_processor`    — reference rewriting + text-ID replacement
- :mod:`pipeline.manifest`          — transformation manifest tagging
- :mod:`pipeline.ports`             — PseudonymizerPort Protocol

Dependency injection: pass a custom ``pseudonymizer`` (any object satisfying
:class:`~pipeline.ports.PseudonymizerPort`) to replace the default gPAS adapter.
All existing callers that omit the parameter continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

from utils.json_fast import dumps_bytes as _json_dumps_bytes, loads as _json_loads
from utils.metrics import PIPELINE_STAGE_LATENCY
from utils.regulated import raw_pii_scan_enabled as _raw_pii_scan_enabled
from utils.thread_pool import get_executor, submit_with_context
from utils.tracing import get_tracer as _get_tracer

from pipeline.correction import quarantine_record
from pipeline.manifest import _MANIFEST_ENABLED, _attach_manifest
from pipeline.rule_matcher import _get_rules_for_resource
from pipeline.action_dispatcher import evaluate_and_dispatch as _evaluate_and_dispatch
from pipeline.gpas_orchestrator import (
    pseudonymize_identifier_batch,
    depseudonymize_resource_identifiers,
    apply_pseudonym_mapping,
    _extract_gpas_params,
)
from pipeline.post_processor import (
    _build_text_id_matcher,
    _collect_reference_ids,
    _post_process_resource,
    _shallow_post_process_bundle,
)


def _tracer():
    return _get_tracer("medanon.pipeline")


@contextmanager
def _stage_span(name: str, *, metric_stage: "str | None" = None):
    """Open a tracing span + Prometheus stage-latency timer for one pipeline stage.

    Both the span and the timer are managed by ``with`` blocks, so they are
    always closed/recorded even when the wrapped body raises. This replaces the
    earlier manual ``__enter__()`` / ``__exit__(None, None, None)`` calls, which
    leaked the span and dropped the latency sample on any exception.

    ``metric_stage`` defaults to ``name`` but may differ when the span name and
    the Prometheus ``stage`` label historically diverged.
    """
    with _tracer().start_as_current_span(name):
        with PIPELINE_STAGE_LATENCY.labels(stage=metric_stage or name).time():
            yield


audit_log = logging.getLogger("medanon.audit")

# The .log() FHIRPath invocation is registered lazily inside
# rule_matcher._compile_fhirpath() on first use — no eager import needed.

# Default 1000: a safe middle ground between the legacy 200 and an aggressive
# 5000.  At 1000 resources/chunk, gPAS and NLP sub-batches fit comfortably
# within their default pool sizes.  Raise via MEDANON_BATCH_SIZE once you have
# confirmed that your gPAS/NLP capacity and thread-pool budget can handle the
# higher concurrency (see GPAS_SUBBATCH_PARALLEL, NLP_CLIENT_SUBBATCH_PARALLEL,
# MEDANON_GLOBAL_MAX_THREADS, MEDANON_PARALLEL_WORKERS).
_BATCH_SIZE = int(os.environ.get("MEDANON_BATCH_SIZE", "1000"))

# Hard cap on Bundle.entry count. A Bundle is processed in a single
# process_data_batch call (see _process_bundle for why it cannot be chunked), so
# its size bounds peak memory and the gPAS request. 0 disables the guard.
# The API paths are already bounded by MEDANON_MAX_BODY_BYTES; this covers the
# CLI and library callers, which are not.
_MAX_BUNDLE_ENTRIES = int(os.environ.get("MEDANON_MAX_BUNDLE_ENTRIES", "50000"))
# Default 8 parallel workers — matches docker-compose.yml default.
# Set to 0 to disable parallelism (sequential processing).
_PARALLEL_WORKERS = int(os.environ.get("MEDANON_PARALLEL_WORKERS", "8"))

# Heuristic attachment scanner in detect_phi_batch — finds Attachment.data /
# *Base64Binary fields anywhere in a resource and scrubs embedded PHI, even when
# no config nlp_* rule targets them.  OFF by default so the config rules are the
# single source of truth: nothing is transformed without a matching rule.  The
# Config Builder's Resource Explorer flags `base64` fields with a recommended
# treatment so users add an explicit nlp_scrub rule (with base64_encoded).
# Opt in with MEDANON_ATTACHMENT_SCAN=true for a config-independent safety net.
_ATTACHMENT_SCAN = os.environ.get(
    "MEDANON_ATTACHMENT_SCAN", "false"
).strip().lower() in (
    "true",
    "1",
    "yes",
    "on",
)

# When set, forces sequential processing and clears all rule caches at the
# start of every process_data_batch call.  Intended for tests only — never
# set in production (disables parallelism and defeats caching).
_DETERMINISTIC = os.environ.get(
    "MEDANON_PIPELINE_DETERMINISTIC", ""
).strip().lower() in (
    "true",
    "1",
)
if _DETERMINISTIC:
    _PARALLEL_WORKERS = 0

__all__ = [
    "process_data",
    "process_data_batch",
    "process_data_stream",
    "_get_default_pseudonymizer",
    "_BATCH_SIZE",
    "PiiLeakError",
]


# Output-gate primitives live in pipeline.gate; re-exported here under their
# historical names so external imports (``from pipeline.processor import
# PiiLeakError``) and test patch targets (``processor._run_pii_gate``) keep
# working unchanged.
from pipeline.gate import (  # noqa: E402
    PiiLeakError,
    quarantine_info_for as _quarantine_info_for,
    run_pii_gate as _run_pii_gate,
)


# ---------------------------------------------------------------------------
# Stage helpers — called directly or dispatched to the thread pool
# ---------------------------------------------------------------------------


def _detect_phi(
    parsed: "list[dict | None]",
    all_nlp_works: "list[list]",
    all_manifest_entries: "list[list]",
    processing_mode: str,
) -> None:
    """phi_detection stage: batch NLP detection and in-place PHI replacement."""
    from pipeline.nlp_orchestrator import detect_phi_batch

    with _stage_span("phi_detection"):
        detect_phi_batch(parsed, all_nlp_works, all_manifest_entries, processing_mode)


def _pseudonymize(
    all_gpas_works: "list[list]",
    processing_mode: str,
    pseudonymizer,
    gpas_params: "dict | None",
    extra_values_by_domain: "dict[str, list[str]] | None",
) -> dict:
    """pseudonymize stage: batch gPAS lookup for all unique values across the chunk."""
    with _stage_span("pseudonymization"):
        return (
            pseudonymize_identifier_batch(
                all_gpas_works,
                processing_mode,
                pseudonymizer,
                gpas_params,
                extra_values_by_domain=extra_values_by_domain,
            )
            or {}
        )


# Module-level singleton — GpasPseudonymizerAdapter is stateless (no instance
# data; all state lives in the module-level gPAS client, circuit breaker, and
# cache). Re-using the same instance avoids per-request object allocation and
# keeps the lazy import pattern to prevent circular imports at module load time.
_default_pseudonymizer = None


def _get_default_pseudonymizer():
    global _default_pseudonymizer
    if _default_pseudonymizer is None:
        from integrations.gpas.adapter import GpasPseudonymizerAdapter

        _default_pseudonymizer = GpasPseudonymizerAdapter()
    return _default_pseudonymizer


def _processing_errors_mode(settings) -> str:
    return str(getattr(settings, "processing_errors", "raise")).lower()


# ---------------------------------------------------------------------------
# finalize stage — gPAS write-back + post-processing (per resource)
# ---------------------------------------------------------------------------


def _assemble_resource(
    resource: dict,
    settings,
    pseudonymizer,
    gpas_work: list,
    manifest_entries: list,
    processing_mode: str,
    precomputed_mapping: dict | None = None,
    precompiled_text_id_regex=None,
    precomputed_ref_mapping: dict | None = None,
    prebuilt_text_id_automaton=None,
    attach_manifest: bool = False,
) -> dict:
    """finalize stage for one resource: gPAS write-back + post-processing.

    Always invoked after the pseudonymize stage has returned the shared mapping
    for the whole batch — zero additional HTTP calls per resource.
    ``precomputed_mapping`` and (when ``rewrite_references`` is on)
    ``precomputed_ref_mapping`` MUST be supplied by the caller.
    """
    if precomputed_mapping is None:
        # Defensive guard — preserves the public signature for the perf bench
        # while making the contract explicit.  See process_data_batch / chunk
        # path which always supply ``shared_mapping``.
        raise ValueError(
            "_assemble_resource requires precomputed_mapping; "
            "call process_data_batch() instead of _assemble_resource directly.",
        )

    # Separate depseudo work items from pseudo work items
    depseudo_work = [
        w for w in gpas_work if w.rule.get("action") == "gpas_depseudonymize"
    ]
    pseudo_work = (
        [w for w in gpas_work if w.rule.get("action") != "gpas_depseudonymize"]
        if depseudo_work
        else gpas_work
    )

    batch_mapping = apply_pseudonym_mapping(
        resource,
        pseudo_work,
        precomputed_mapping,
        processing_mode,
        manifest_entries=manifest_entries,
    )

    # Batch de-pseudonymization (separate from pseudonymization)
    if depseudo_work:
        depseudonymize_resource_identifiers(resource, depseudo_work, processing_mode)

    # Post-processing: determine what needs rewriting
    do_refs = getattr(settings, "rewrite_references", False)
    do_text_ids = getattr(settings, "rewrite_text_ids", False) and batch_mapping

    id_text_map = None
    if do_text_ids:
        id_text_map = {
            k: v
            for k, v in batch_mapping.items()
            if k and v and k != v and not k.startswith("{")
        }
        if not id_text_map:
            id_text_map = None
        else:
            audit_log.debug("rewriting_text_ids count=%d", len(id_text_map))

    # Single-walk post-processing: ref pseudonymisation + text-ID replacement
    # share one tree traversal via ``_post_process_resource`` (the legacy
    # apply-refs walk + text-ID walk were merged in an earlier refactor).
    ref_mapping_to_apply = precomputed_ref_mapping if do_refs else None
    if ref_mapping_to_apply or id_text_map:
        _post_process_resource(
            resource,
            ref_mapping=ref_mapping_to_apply,
            id_map=id_text_map,
            automaton=prebuilt_text_id_automaton,
            compiled=precompiled_text_id_regex,
        )

    # Attach transformation manifest (when enabled or forced for scoring)
    if (attach_manifest or _MANIFEST_ENABLED) and manifest_entries:
        _attach_manifest(resource, manifest_entries)

    return resource


# ---------------------------------------------------------------------------
# Batch processing (unified)
# ---------------------------------------------------------------------------


def _evaluate_rules(resource, settings, processing_mode, collect_refs=False):
    """match stage for a single resource — suitable for thread pool dispatch.

    Evaluates FHIRPath rules and dispatches actions, accumulating deferred
    gPAS (PseudonymizationTask) and NLP (PHIDetectionTask) items for batch processing.

    In ``skip`` mode, lazily snapshots the resource before mutation so that a
    partially de-identified resource is never emitted (PHI leak prevention).
    The snapshot is only taken when the resource has matching rules (i.e.,
    mutations will actually occur), avoiding JSON allocation for the common
    success path and for resources with no matching rules.

    When *collect_refs* is True, collects reference IDs from the resource
    **before** the match stage mutates it so that original (pre-pseudonymisation)
    IDs are captured. Reference collection runs inside the thread pool to avoid
    a serial pass after all match-stage work completes.
    """
    snapshot = None  # populated lazily below, after rules are known
    try:
        rules = _get_rules_for_resource(resource, settings)
        # Lazy snapshot: only serialize when rules exist and will mutate this
        # resource.  Avoids JSON allocation for the ~99.99% success path and
        # for resources with no matching rules (no mutations possible).
        if processing_mode == "skip" and rules:
            snapshot = _json_dumps_bytes(resource)
        manifest_entries: list[dict] = []
        # Collect reference IDs BEFORE the match stage mutates the resource so
        # that outbound references capture original (pre-pseudonymisation) IDs.
        # ref_type_map: {bare_id: resource_type} — enables per-type domain routing.
        ref_type_map: "dict[str, str] | None" = None
        if collect_refs:
            _ref_ids: set[str] = set()
            ref_type_map = {}
            _collect_reference_ids(resource, _ref_ids, ref_types=ref_type_map)
            # Include urn:uuid: IDs (not in ref_type_map) under type ""
            # so the N>1 batch path routes them to the default gPAS domain.
            for _rid in _ref_ids:
                if _rid not in ref_type_map:
                    ref_type_map[_rid] = ""
        gpas_work, nlp_work = _evaluate_and_dispatch(
            resource, rules, settings, manifest_entries, processing_mode
        )
        return resource, gpas_work, nlp_work, manifest_entries, ref_type_map
    except Exception as original_exc:
        if snapshot is not None:
            # Restore original resource to prevent emitting partial de-identification.
            # If restoration itself fails the resource is cleared (empty output) rather
            # than left in a partially de-identified state — never leak PHI.
            try:
                resource.clear()
                resource.update(_json_loads(snapshot))
            except Exception as restore_exc:
                audit_log.error(
                    "snapshot_restore_failed resource_type=%s — resource cleared to prevent PHI leak: %s",
                    resource.get("resourceType", "unknown"),
                    restore_exc,
                )
                resource.clear()
        raise original_exc


def _run_finalize_stage(
    parsed,
    all_gpas_works,
    all_nlp_works,
    all_manifest_entries,
    settings,
    pseudonymizer,
    shared_mapping,
    _batch_text_id_regex,
    _batch_text_id_automaton,
    _batch_ref_mapping,
    attach_manifest,
    processing_mode,
    _keep_manifest,
    quarantine_info: "dict[int, dict] | None" = None,
):
    """Stage 4 — finalize: gPAS write-back + post-processing per resource.

    Each resource is independent (``shared_mapping`` is read-only here), so the
    work is dispatched to the thread pool for batches > 4 resources and run
    sequentially otherwise. Intermediate per-resource data is freed as it is
    consumed to cap peak memory, even when an exception propagates.
    """
    results: list[dict | None] = [None] * len(parsed)
    n_resources = len(parsed)

    if n_resources > 4 and _PARALLEL_WORKERS > 0:
        # Parallel finalization: each resource is independent (shared_mapping is read-only)
        pool = get_executor()
        futures = {}
        for i, (resource, gpas_work, manifest_entries_) in enumerate(
            zip(parsed, all_gpas_works, all_manifest_entries)
        ):
            if resource is None:
                results[i] = quarantine_record(
                    error="match stage error",
                    stage="match",
                    **(quarantine_info or {}).get(i, {}),
                )
                continue
            try:
                # submit_with_context — _assemble_resource may call
                # depseudonymize_resource_identifiers, which reads the active
                # permit context (pipeline.permit_context) for domain scoping.
                fut = submit_with_context(
                    pool,
                    _assemble_resource,
                    resource,
                    settings,
                    pseudonymizer,
                    gpas_work,
                    manifest_entries_,
                    processing_mode,
                    precomputed_mapping=shared_mapping,
                    precompiled_text_id_regex=_batch_text_id_regex,
                    precomputed_ref_mapping=_batch_ref_mapping,
                    prebuilt_text_id_automaton=_batch_text_id_automaton,
                    attach_manifest=attach_manifest,
                )
                futures[fut] = i
            except TimeoutError:
                audit_log.warning(
                    "resource_assembly_worker_timeout: thread pool saturated at resource %d/%d, "
                    "falling back to sequential",
                    i,
                    n_resources,
                )
                # Finalize remaining resources sequentially after draining futures
                _finalize_sequential_start = i
                break
        else:
            _finalize_sequential_start = None

        from concurrent.futures import as_completed

        try:
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as exc:
                    resource = parsed[i]
                    rtype = (
                        resource.get("resourceType", "Unknown")
                        if isinstance(resource, dict)
                        else "Unknown"
                    )
                    audit_log.error(
                        "resource_assembly_error resource_type=%s: %s", rtype, exc
                    )
                    if processing_mode != "skip":
                        raise
                    results[i] = quarantine_record(
                        error="processing error",
                        resource_type=rtype,
                        stage="finalize",
                        error_type=type(exc).__name__,
                    )

            if _finalize_sequential_start is not None:
                for i, (resource, gpas_work, manifest_entries_) in enumerate(
                    zip(
                        parsed[_finalize_sequential_start:],
                        all_gpas_works[_finalize_sequential_start:],
                        all_manifest_entries[_finalize_sequential_start:],
                    ),
                    start=_finalize_sequential_start,
                ):
                    if resource is None:
                        results[i] = quarantine_record(
                            error="match stage error",
                            stage="match",
                            **(quarantine_info or {}).get(i, {}),
                        )
                        continue
                    try:
                        results[i] = _assemble_resource(
                            resource,
                            settings,
                            pseudonymizer,
                            gpas_work,
                            manifest_entries_,
                            processing_mode,
                            precomputed_mapping=shared_mapping,
                            precompiled_text_id_regex=_batch_text_id_regex,
                            precomputed_ref_mapping=_batch_ref_mapping,
                            prebuilt_text_id_automaton=_batch_text_id_automaton,
                            attach_manifest=attach_manifest,
                        )
                    except Exception as exc:
                        rtype = (
                            resource.get("resourceType", "Unknown")
                            if isinstance(resource, dict)
                            else "Unknown"
                        )
                        audit_log.error(
                            "resource_assembly_error resource_type=%s: %s", rtype, exc
                        )
                        if processing_mode != "skip":
                            raise
                        results[i] = quarantine_record(
                            error="processing error",
                            resource_type=rtype,
                            stage="finalize",
                            error_type=type(exc).__name__,
                        )
        finally:
            # Always free intermediate per-resource data to cap peak memory,
            # even when an exception propagates (e.g. processing_mode=raise).
            for i in range(n_resources):
                parsed[i] = None
                all_gpas_works[i] = []
                all_nlp_works[i] = []
                if not _keep_manifest:
                    all_manifest_entries[i] = []
    else:
        # Small batch — sequential (no thread pool overhead)
        for i, (resource, gpas_work, manifest_entries_) in enumerate(
            zip(parsed, all_gpas_works, all_manifest_entries)
        ):
            if resource is None:
                results[i] = quarantine_record(
                    error="match stage error",
                    stage="match",
                    **(quarantine_info or {}).get(i, {}),
                )
            else:
                try:
                    result = _assemble_resource(
                        resource,
                        settings,
                        pseudonymizer,
                        gpas_work,
                        manifest_entries_,
                        processing_mode,
                        precomputed_mapping=shared_mapping,
                        precompiled_text_id_regex=_batch_text_id_regex,
                        precomputed_ref_mapping=_batch_ref_mapping,
                        prebuilt_text_id_automaton=_batch_text_id_automaton,
                        attach_manifest=attach_manifest,
                    )
                    results[i] = result
                except Exception as exc:
                    rtype = (
                        resource.get("resourceType", "Unknown")
                        if isinstance(resource, dict)
                        else "Unknown"
                    )
                    audit_log.error(
                        "resource_assembly_error resource_type=%s: %s", rtype, exc
                    )
                    if processing_mode != "skip":
                        raise
                    results[i] = quarantine_record(
                        error="processing error",
                        resource_type=rtype,
                        stage="finalize",
                        error_type=type(exc).__name__,
                    )
            # Free intermediate data for this resource to reduce peak memory
            parsed[i] = None
            all_gpas_works[i] = []
            all_nlp_works[i] = []
            if not _keep_manifest:
                all_manifest_entries[i] = []

    return results


def process_data_batch(
    resources: list[dict],
    settings,
    pseudonymizer=None,
    attach_manifest: bool = False,
    _return_manifest: bool = False,
) -> "list[dict] | tuple[list[dict], list[list[dict]]]":
    """De-identify / pseudonymize a batch of FHIR resources.

    Runs four pipeline stages:

    1. **match** — FHIRPath evaluation + action dispatch per resource (parallel).
    2. **phi_detection** + **pseudonymization** — NLP replacement and gPAS batch lookup
       run **concurrently**: NLP modifies free-text fields while gPAS fetches
       pseudonyms for structured identifiers. Both stages consume only match-stage
       output and write to disjoint resource paths, so concurrent execution is safe.
    3. **finalize** — gPAS write-back + post-processing per resource (parallel).

    Cross-chunk pseudonym dedup relies on the gPAS client's deterministic L1 LRU
    and Redis L2 cache: identical values always map to identical pseudonyms, so
    duplicate HTTP calls are cache hits rather than new entries.

    Args:
        resources:        List of FHIR resource dicts (not Bundles — handled by
                          ``_process_bundle``).
        settings:         Loaded :class:`~pipeline.config.Settings` instance.
        pseudonymizer:    Optional :class:`~pipeline.ports.PseudonymizerPort`.
        _return_manifest: When True, return ``(results, manifest_entries_list)``
                          instead of just ``results``. The caller can pass the
                          pre-parsed entries directly to the scoring summary,
                          avoiding a JSON re-parse from ``meta.tag``.

    Returns:
        List of processed resource dicts (same length and order as input), or
        a ``(results, manifest_entries_list)`` tuple when *_return_manifest* is True.
    """
    if pseudonymizer is None:
        pseudonymizer = _get_default_pseudonymizer()

    if not resources:
        return []

    if _DETERMINISTIC:
        from pipeline.rule_matcher import clear_rule_caches

        clear_rule_caches()

    processing_mode = _processing_errors_mode(settings)

    gpas_params = _extract_gpas_params(settings)
    _need_refs = bool(getattr(settings, "rewrite_references", False) and gpas_params)

    parsed: list[dict | None] = []
    all_gpas_works: list[list] = []
    all_nlp_works: list[list] = []
    all_manifest_entries: list[list] = []
    # PHI-free identity info for resources quarantined in the match stage,
    # keyed by parsed index — consumed by _run_finalize_stage so quarantine
    # records are traceable to a source resource (1.6).
    quarantine_info: dict[int, dict] = {}
    _all_ref_type_map: dict[
        str, str
    ] = {}  # ref_id → resource_type for per-domain routing

    # Stage 1 — match: FHIRPath evaluation + action dispatch (parallel per resource).
    # Reference IDs are collected during the same call so they benefit from
    # thread pool parallelism rather than running serially afterward.
    #
    # The whole instrumented body runs inside a try/finally that guarantees the
    # batch span is closed on every exit path (success, return, or raise). The
    # per-stage spans + latency timers use the ``_stage_span`` context manager,
    # which likewise closes the span and records the sample even when the stage
    # body raises (the previous manual ``__enter__``/``__exit__`` pairs leaked
    # the span and dropped the metric on any exception).
    _batch_span_cm = _tracer().start_as_current_span("pipeline.batch")
    _batch_span_cm.__enter__()
    try:
        with _stage_span("rule_evaluation"):
            if _PARALLEL_WORKERS > 0:
                pool = get_executor()
                futures = []
                _parallel_fell_back = False
                for resource in resources:
                    try:
                        # submit_with_context — _evaluate_rules dispatches
                        # cryptohash/tokenize/date_shift, which read the
                        # active permit context (pipeline.permit_context) to
                        # scope their derived keys per permit.
                        futures.append(
                            submit_with_context(
                                pool,
                                _evaluate_rules,
                                resource,
                                settings,
                                processing_mode,
                                collect_refs=_need_refs,
                            )
                        )
                    except TimeoutError:
                        audit_log.warning(
                            "rule_evaluation_worker_timeout: thread pool saturated after %d/%d resources, "
                            "falling back to sequential",
                            len(futures),
                            len(resources),
                        )
                        _parallel_fell_back = True
                        break
                for idx, future in enumerate(futures):
                    try:
                        (
                            resource,
                            gpas_work,
                            nlp_work,
                            manifest_entries_,
                            ref_type_map_,
                        ) = future.result()
                        parsed.append(resource)
                        all_gpas_works.append(gpas_work)
                        all_nlp_works.append(nlp_work)
                        all_manifest_entries.append(manifest_entries_)
                        if ref_type_map_:
                            _all_ref_type_map.update(ref_type_map_)
                    except Exception as exc:
                        audit_log.error(
                            "rule_evaluation_error error_type=%s", type(exc).__name__
                        )
                        if processing_mode != "skip":
                            raise
                        quarantine_info[idx] = _quarantine_info_for(resources[idx], exc)
                        parsed.append(None)
                        all_gpas_works.append([])
                        all_nlp_works.append([])
                        all_manifest_entries.append([])
                if _parallel_fell_back:
                    for resource in resources[len(futures) :]:
                        try:
                            (
                                resource,
                                gpas_work,
                                nlp_work,
                                manifest_entries_,
                                ref_type_map_,
                            ) = _evaluate_rules(
                                resource,
                                settings,
                                processing_mode,
                                collect_refs=_need_refs,
                            )
                        except Exception as exc:
                            audit_log.error(
                                "rule_evaluation_error (sequential fallback) error_type=%s",
                                type(exc).__name__,
                            )
                            if processing_mode != "skip":
                                raise
                            quarantine_info[len(parsed)] = _quarantine_info_for(
                                resource, exc
                            )
                            parsed.append(None)
                            all_gpas_works.append([])
                            all_nlp_works.append([])
                            all_manifest_entries.append([])
                            continue
                        parsed.append(resource)
                        all_gpas_works.append(gpas_work)
                        all_nlp_works.append(nlp_work)
                        all_manifest_entries.append(manifest_entries_)
                        if ref_type_map_:
                            _all_ref_type_map.update(ref_type_map_)
            else:
                for resource in resources:
                    try:
                        (
                            resource,
                            gpas_work,
                            nlp_work,
                            manifest_entries_,
                            ref_type_map_,
                        ) = _evaluate_rules(
                            resource,
                            settings,
                            processing_mode,
                            collect_refs=_need_refs,
                        )
                    except Exception as exc:
                        rtype = (
                            resource.get("resourceType", "Unknown")
                            if isinstance(resource, dict)
                            else "Unknown"
                        )
                        audit_log.error(
                            "rule_evaluation_error resource_type=%s error_type=%s",
                            rtype,
                            type(exc).__name__,
                        )
                        if processing_mode != "skip":
                            raise
                        quarantine_info[len(parsed)] = _quarantine_info_for(
                            resource, exc
                        )
                        parsed.append(None)
                        all_gpas_works.append([])
                        all_nlp_works.append([])
                        all_manifest_entries.append([])
                        continue

                    parsed.append(resource)
                    all_gpas_works.append(gpas_work)
                    all_nlp_works.append(nlp_work)
                    all_manifest_entries.append(manifest_entries_)
                    if ref_type_map_:
                        _all_ref_type_map.update(ref_type_map_)

        # Build per-domain reference ID buckets for typed domain routing.
        # Uses settings.domain_map so Patient references → spe.direct.patient-admin
        # rather than the default domain (which would produce a different pseudonym).
        _extra_values_by_domain: "dict[str, list[str]] | None" = None
        if _all_ref_type_map:
            _domain_map = getattr(settings, "domain_map", None)
            _def_domain = gpas_params.get("gpas_domain", "") if gpas_params else ""
            _extra_values_by_domain = {}
            for _rid, _rtype in _all_ref_type_map.items():
                _dom = (_domain_map or {}).get(_rtype, _def_domain)
                _extra_values_by_domain.setdefault(_dom, []).append(_rid)

        return _finalize_batch(
            resources,
            settings,
            pseudonymizer,
            attach_manifest,
            _return_manifest,
            parsed,
            all_gpas_works,
            all_nlp_works,
            all_manifest_entries,
            _all_ref_type_map,
            _extra_values_by_domain,
            gpas_params,
            processing_mode,
            quarantine_info=quarantine_info,
        )
    finally:
        _batch_span_cm.__exit__(None, None, None)


def _finalize_batch(
    resources,
    settings,
    pseudonymizer,
    attach_manifest,
    _return_manifest,
    parsed,
    all_gpas_works,
    all_nlp_works,
    all_manifest_entries,
    _all_ref_type_map,
    _extra_values_by_domain,
    gpas_params,
    processing_mode,
    quarantine_info: "dict[int, dict] | None" = None,
):
    """Stages 2–4 of :func:`process_data_batch` (phi_detection ‖ pseudonymize, finalize).

    Split out of ``process_data_batch`` so the match stage's ``with`` block stays
    readable. Runs inside the caller's open ``pipeline.batch`` span.
    """
    # Stages 2 + 3 — phi_detection (NLP) and pseudonymization (gPAS) run concurrently.
    # NLP processes free-text/narrative fields including heuristic attachment scans.
    # gPAS pre-fetches pseudonyms for structured identifiers and references.
    #
    # Concurrency safety: the gPAS call (``_pseudonymize`` →
    # ``pseudonymize_identifier_batch``) is READ-ONLY with respect to the resource
    # dicts — it collects serialized values, calls the pseudonymizer, and returns a
    # mapping dict without touching any resource.  Write-back happens in stage 4
    # (``_assemble_resource``) AFTER the NLP thread is joined.  The NLP stage
    # mutates free-text fields in place, but gPAS write-back hasn't started yet, so
    # the two stages never contend on the same dict keys.  This is what makes
    # concurrent execution safe — NOT disjoint paths (the NLP heuristic scanner
    # walks the entire resource tree).
    #
    # NLP runs in a dedicated thread; gPAS runs in the caller thread. Using
    # threading.Thread directly avoids competing for slots in the shared
    # get_executor() pool (which is already used by match and finalize stages).
    _has_nlp = any(all_nlp_works)
    # Run the NLP stage when there is config-rule NLP work OR when the
    # heuristic attachment scanner is enabled (MEDANON_ATTACHMENT_SCAN=true,
    # the default).  The heuristic scanner runs inside detect_phi_batch
    # regardless of config-rule NLP work, but it needs the NLP adapter to
    # be available.  We do a cheap cached adapter check here to avoid
    # launching an unnecessary thread when NLP is not configured at all.
    _nlp_adapter_available = False
    if _ATTACHMENT_SCAN:
        from pipeline.deidentify import _get_nlp_adapter

        _nlp_adapter_available = _get_nlp_adapter() is not None
    _run_nlp_stage = _has_nlp or _nlp_adapter_available

    _nlp_thread: "threading.Thread | None" = None
    _nlp_exc: "BaseException | None" = None

    if _PARALLEL_WORKERS > 0 and _run_nlp_stage:

        def _nlp_target() -> None:
            nonlocal _nlp_exc
            try:
                _detect_phi(
                    parsed, all_nlp_works, all_manifest_entries, processing_mode
                )
            except BaseException as exc:
                _nlp_exc = exc

        _nlp_thread = threading.Thread(
            target=_nlp_target, daemon=True, name="medanon-enrich-nlp"
        )
        _nlp_thread.start()

    # gPAS runs in the caller thread while NLP runs concurrently above.
    # If the gPAS stage raises, we MUST still join the NLP thread — otherwise it
    # keeps mutating the ``parsed`` resource dicts after the batch has logically
    # failed (an orphaned writer). The try/finally guarantees the join happens on
    # every exit path before the exception propagates.
    try:
        shared_mapping = _pseudonymize(
            all_gpas_works,
            processing_mode,
            pseudonymizer,
            gpas_params,
            _extra_values_by_domain,
        )
    finally:
        if _nlp_thread is not None:
            _nlp_thread.join()

    if _nlp_thread is not None:
        if _nlp_exc is not None:
            raise _nlp_exc  # type: ignore[misc]
    elif _run_nlp_stage:
        # PARALLEL_WORKERS=0 (deterministic mode) — run NLP sequentially
        _detect_phi(parsed, all_nlp_works, all_manifest_entries, processing_mode)

    # Pre-compile the text-ID matcher once for the whole batch so each resource
    # doesn't rebuild independently on the same pattern.
    # Prefer Aho-Corasick (O(N+M)) when available; fall back to regex.
    _batch_text_id_regex = None
    _batch_text_id_automaton = None
    if getattr(settings, "rewrite_text_ids", False) and shared_mapping:
        _batch_id_text_map = {
            k: v
            for k, v in shared_mapping.items()
            if k and v and k != v and not k.startswith("{")
        }
        if _batch_id_text_map:
            _batch_text_id_automaton, _batch_text_id_regex = _build_text_id_matcher(
                _batch_id_text_map
            )

    # Reference pseudonym mapping comes from shared_mapping (same HTTP call).
    _batch_ref_mapping: dict | None = shared_mapping if _all_ref_type_map else None

    # Stage 4 — finalize: gPAS write-back + post-processing per resource (parallel).
    # shared_mapping is read-only here; each resource is independent.
    #
    # ``_run_finalize_stage`` frees each resource's manifest entries as it
    # consumes them, to cap peak memory. The output gate's structural check
    # reads those entries (which paths were transformed), and gPAS write-back
    # appends to them *during* this stage — so they are only complete once it
    # returns. Keep them alive across the gate call when the check will run.
    _keep_manifest = _return_manifest or (_MANIFEST_ENABLED and _raw_pii_scan_enabled())

    with _stage_span("resource_assembly"):
        results = _run_finalize_stage(
            parsed,
            all_gpas_works,
            all_nlp_works,
            all_manifest_entries,
            settings,
            pseudonymizer,
            shared_mapping,
            _batch_text_id_regex,
            _batch_text_id_automaton,
            _batch_ref_mapping,
            attach_manifest,
            processing_mode,
            _keep_manifest,
            quarantine_info=quarantine_info,
        )

    # Output barrier — runs at the single choke point that all callers share:
    # batch API, NDJSON streaming, async bulk/cohort jobs, staged worker, and
    # Bundle inner processing all call process_data_batch.
    # Default-ON; disable with MEDANON_OUTPUT_GATE_ENABLED=false (or skip the
    # raw scan only with MEDANON_PII_GATE=false).
    if _keep_manifest:
        _gate_results: list[dict] = []
        _gate_manifests: list[list[dict]] = []
        for _r, _m in zip(results, all_manifest_entries):
            if _r is not None:
                _gate_results.append(_r)
                _gate_manifests.append(_m)
        _run_pii_gate(_gate_results, _gate_manifests, settings)
    else:
        # No manifest available — the structural check no-ops, content scan only.
        _run_pii_gate([r for r in results if r is not None])

    if _return_manifest:
        return results, all_manifest_entries
    return results


# ---------------------------------------------------------------------------
# Bundle processing
# ---------------------------------------------------------------------------


def _process_bundle(
    resource: dict, settings, pseudonymizer, attach_manifest: bool = False
) -> dict:
    entries = resource.get("entry", [])

    # Snapshot original resource IDs before any processing
    pre_ids: list[tuple] = []
    inner_resources: list[dict] = []
    entry_indices: list[int] = []
    for i, entry in enumerate(entries):
        r = entry.get("resource", {}) if isinstance(entry, dict) else {}
        if isinstance(r, dict) and "resourceType" in r:
            if "id" in r:
                pre_ids.append((r["resourceType"], r["id"]))
            else:
                pre_ids.append((r.get("resourceType"), None))
            inner_resources.append(r)
            entry_indices.append(i)
        else:
            pre_ids.append((None, None))

    # All inner resources go through in ONE process_data_batch call, deliberately:
    # gPAS dedup and the ``bundle`` NLP token scope are both per-call, so chunking
    # here would split a Bundle's pseudonym namespace and renumber its surrogates.
    #
    # ``process_data_batch`` does NOT chunk (``_BATCH_SIZE`` is only the default
    # chunk size of ``process_data_stream``), so N entries mean N futures and an
    # N-value gPAS request. The API paths cap the input at ``MEDANON_MAX_BODY_BYTES``
    # (10 MB), but the CLI and library paths have no such bound, hence the explicit
    # guard below rather than a comment claiming a chunking that does not exist.
    if inner_resources:
        max_entries = _MAX_BUNDLE_ENTRIES
        if max_entries and len(inner_resources) > max_entries:
            raise ValueError(
                f"Bundle has {len(inner_resources)} entries, over the "
                f"MEDANON_MAX_BUNDLE_ENTRIES limit of {max_entries}. Split it, or "
                f"stream it as NDJSON via process_data_stream, which is chunked."
            )
        all_processed = process_data_batch(
            inner_resources, settings, pseudonymizer, attach_manifest=attach_manifest
        )
        for idx, result in zip(entry_indices, all_processed):
            entries[idx]["resource"] = result

    # Build reference mapping from IDs that changed during processing
    ref_map: dict[str, str] = {}
    res_idx = 0
    for i, entry in enumerate(entries):
        old_type, old_id = pre_ids[i]
        if old_type is None:
            continue
        if i in entry_indices:
            r = entry.get("resource", {}) if isinstance(entry, dict) else {}
            new_id = r.get("id") if isinstance(r, dict) else None
            if old_id is not None and new_id is not None and old_id != new_id:
                ref_map[f"{old_type}/{old_id}"] = f"{old_type}/{new_id}"
            res_idx += 1

    if ref_map:
        audit_log.info("rewriting_references count=%d", len(ref_map))
        # Convert Type/old → Type/new ref_map to bare-ID mapping for the
        # merged single-walk path, and build text-ID map simultaneously.
        bare_id_map: dict[str, str] = {}
        id_text_map: dict[str, str] = {}
        for old_ref, new_ref in ref_map.items():
            old_id = old_ref.split("/", 1)[-1]
            new_id = new_ref.split("/", 1)[-1]
            if old_id and new_id and old_id != new_id:
                bare_id_map[old_id] = new_id
                id_text_map[old_id] = new_id

        do_text_ids = getattr(settings, "rewrite_text_ids", False) and id_text_map
        if do_text_ids:
            # Text-ID replacement requires a full walk of all Bundle content
            # including entry[].resource (cross-resource text refs not fixed
            # during individual processing).
            automaton, compiled = _build_text_id_matcher(id_text_map)
            audit_log.debug("rewriting_text_ids count=%d", len(id_text_map))
            _post_process_resource(
                resource,
                ref_mapping=bare_id_map or None,
                id_map=id_text_map,
                automaton=automaton,
                compiled=compiled,
            )
        elif bare_id_map:
            # Reference-only rewrite: skip entry[].resource — individual resource
            # processing already applied the shared pseudonym mapping to every
            # reference field.  Only Bundle-level metadata needs updating.
            _shallow_post_process_bundle(resource, ref_mapping=bare_id_map)
    elif getattr(settings, "rewrite_text_ids", False):
        # No ref_map but text_ids requested — nothing to rewrite
        pass

    return resource


# ---------------------------------------------------------------------------
# Public entry point (backward-compatible)
# ---------------------------------------------------------------------------


def _rewrite_list_references(pre_ids, results, settings) -> None:
    """Map-based cross-resource reference rewriting for a flat LIST/array input.

    ``process_data_batch`` only rewrites references via the gPAS shared mapping
    (``_need_refs = rewrite_references and gpas_params``). For non-gPAS, in-place
    actions (tokenize, cryptohash, …) on a plain array, references to sibling
    resources are otherwise left pointing at the original IDs. This mirrors what
    ``_process_bundle`` already does for Bundle input: build a ``Type/old →
    Type/new`` map from each resource's id change and rewrite every reference.

    Skipped when ``rewrite_references`` is off, or when gPAS is configured (the
    batch path already folded reference IDs into the pseudonym call — doing it
    again here would be redundant). Mutates *results* in place.
    """
    if not getattr(settings, "rewrite_references", False):
        return
    if _extract_gpas_params(settings):
        return  # gPAS path already handled references in the same batch call

    bare_id_map: dict[str, str] = {}
    for (old_type, old_id), r in zip(pre_ids, results):
        if not old_type or old_id is None or not isinstance(r, dict):
            continue
        new_id = r.get("id")
        if isinstance(new_id, str) and new_id and new_id != old_id:
            bare_id_map[old_id] = new_id
    if not bare_id_map:
        return

    audit_log.info("rewriting_references count=%d (list path)", len(bare_id_map))
    do_text_ids = getattr(settings, "rewrite_text_ids", False)
    automaton = compiled = None
    if do_text_ids:
        automaton, compiled = _build_text_id_matcher(bare_id_map)
    for r in results:
        if isinstance(r, dict):
            _post_process_resource(
                r,
                ref_mapping=bare_id_map,
                id_map=bare_id_map if do_text_ids else None,
                automaton=automaton,
                compiled=compiled,
            )


def process_data(resource, settings, pseudonymizer=None, attach_manifest: bool = False):
    """De-identify / pseudonymize *resource* according to *settings*.

    Args:
        resource:        A FHIR resource dict, a Bundle dict, or a list of resources.
        settings:        Loaded :class:`~pipeline.config.Settings` instance.
        pseudonymizer:   Optional :class:`~pipeline.ports.PseudonymizerPort` override.
                         Defaults to :class:`~integrations.gpas.adapter.GpasPseudonymizerAdapter`.
        attach_manifest: When True, attaches transformation manifests to every output
                         resource (required for manifest-based summary in the UI).

    Returns:
        The transformed resource (same type as input).
    """
    if pseudonymizer is None:
        pseudonymizer = _get_default_pseudonymizer()
    if isinstance(resource, list):
        # Capture original (type, id) BEFORE processing so cross-resource
        # references can be rewritten for non-gPAS actions (tokenize/cryptohash)
        # the same way Bundle input is handled.
        _pre_ids = [
            (r.get("resourceType"), r.get("id"))
            if isinstance(r, dict)
            else (None, None)
            for r in resource
        ]
        results = process_data_batch(
            resource, settings, pseudonymizer, attach_manifest=attach_manifest
        )
        _rewrite_list_references(_pre_ids, results, settings)
        return results
    if isinstance(resource, dict) and resource.get("resourceType") == "Bundle":
        return _process_bundle(
            resource, settings, pseudonymizer, attach_manifest=attach_manifest
        )
    return process_data_batch(
        [resource], settings, pseudonymizer, attach_manifest=attach_manifest
    )[0]


def process_data_stream(
    resources_iter, settings, pseudonymizer=None, chunk_size=None, attach_manifest=False
):
    """Generator that de-identifies resources from *resources_iter* in chunks.

    Yields one processed resource dict at a time. Memory usage is bounded by
    ``chunk_size × avg_resource_size`` instead of growing with the total input.

    Cross-chunk pseudonym dedup relies on the gPAS client's deterministic L1 LRU
    and Redis L2 cache: identical values always map to identical pseudonyms, so
    duplicate HTTP calls across chunks are cache hits rather than new gPAS entries.

    Args:
        resources_iter: Iterable of FHIR resource dicts (not Bundles).
        settings:       Loaded Settings instance.
        pseudonymizer:  Optional PseudonymizerPort override.
        chunk_size:     Resources per batch (default: ``_BATCH_SIZE``).
        attach_manifest: Always attach transformation manifests (for scoring support).

    Yields:
        Processed resource dicts, one at a time.
    """
    if pseudonymizer is None:
        pseudonymizer = _get_default_pseudonymizer()
    if chunk_size is None:
        chunk_size = _BATCH_SIZE

    chunk: list[dict] = []
    for resource in resources_iter:
        chunk.append(resource)
        if len(chunk) >= chunk_size:
            yield from process_data_batch(
                chunk,
                settings,
                pseudonymizer,
                attach_manifest=attach_manifest,
            )
            chunk = []
    if chunk:
        yield from process_data_batch(
            chunk,
            settings,
            pseudonymizer,
            attach_manifest=attach_manifest,
        )
