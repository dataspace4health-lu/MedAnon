"""FHIR de-identification / pseudonymization orchestrator.

Public surface:

- :func:`process_data` — single resource / Bundle / list (signature unchanged).
- :func:`process_data_batch` — batch of resources with cross-resource gPAS
  batching (one HTTP call for up to ``MEDANON_BATCH_SIZE`` resources).

All logic is delegated to the focused pipeline sub-modules:

- :mod:`pipeline.rule_matcher`    — FHIRPath evaluation + rule index
- :mod:`pipeline.action_dispatcher` — Pass 1 action dispatch + BatchWork accumulation
- :mod:`pipeline.gpas_orchestrator` — Pass 2 batch gPAS call
- :mod:`pipeline.post_processor`  — reference rewriting + text-ID replacement
- :mod:`pipeline.manifest`        — transformation manifest tagging
- :mod:`pipeline.ports`           — PseudonymizerPort Protocol

Dependency injection: pass a custom ``pseudonymizer`` (any object satisfying
:class:`~pipeline.ports.PseudonymizerPort`) to replace the default gPAS adapter.
All existing callers that omit the parameter continue to work unchanged.
"""

from __future__ import annotations

import logging
import os

from utils.json_fast import dumps_bytes as _json_dumps_bytes, loads as _json_loads
from utils.thread_pool import get_executor

from pipeline.manifest import _MANIFEST_ENABLED, _attach_manifest
from pipeline.rule_matcher import _get_rules_for_resource
from pipeline.action_dispatcher import dispatch_pass1
from pipeline.gpas_orchestrator import (
    run_gpas_batch,
    run_gpas_batch_for_batch,
    run_gpas_depseudo_batch,
    write_back_gpas_batch,
    _extract_gpas_params,
)
from pipeline.post_processor import (
    _build_text_id_matcher,
    _deep_rewrite_references_gpas,
    _post_process_resource,
    _rewrite_references,
    _rewrite_text_ids,
    _collect_reference_ids,
)
from integrations.gpas.circuit_breaker import GpasUnavailableError

audit_log = logging.getLogger("medanon.audit")

# The .log() FHIRPath invocation is registered lazily inside
# rule_matcher._compile_fhirpath() on first use — no eager import needed.

_BATCH_SIZE = int(os.environ.get("MEDANON_BATCH_SIZE", "1000"))
# Default 8 parallel workers — matches docker-compose.yml default.
# Set to 0 to disable parallelism (sequential processing).
_PARALLEL_WORKERS = int(os.environ.get("MEDANON_PARALLEL_WORKERS", "8"))

_SEEN_VALUES_CAP = int(os.environ.get("MEDANON_SEEN_VALUES_CAP", "200000"))


class _CappedSet:
    """A set that stops accepting new entries once a capacity limit is reached.

    After the cap is hit, ``add()`` and ``update()`` silently drop new values.
    Existing values remain queryable via ``__contains__``.  This is safe because
    the set is an optimization hint only — the L1 gPAS cache is the
    authoritative dedup layer and will still serve cache hits for values
    not in this set.
    """

    __slots__ = ("_set", "_cap", "_frozen")

    def __init__(self, cap: int = _SEEN_VALUES_CAP):
        self._set: set[str] = set()
        self._cap = cap
        self._frozen = False

    def __contains__(self, item) -> bool:
        return item in self._set

    def add(self, item: str) -> None:
        if self._frozen:
            return
        self._set.add(item)
        if len(self._set) >= self._cap:
            self._frozen = True

    def update(self, items) -> None:
        if self._frozen:
            return
        self._set.update(items)
        if len(self._set) >= self._cap:
            self._frozen = True

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
# Per-resource finalize (Pass 2 + post-processing)
# ---------------------------------------------------------------------------


def _finalize_resource(
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
    """Run Pass 2 (gPAS) + post-processing for one resource.

    When *precomputed_mapping* is provided (N>1 batch path), applies the
    shared mapping directly with **zero** additional HTTP calls — used after
    :func:`run_gpas_batch_for_batch` has already fetched all pseudonyms.

    When *precomputed_ref_mapping* is provided, applies it directly instead
    of re-collecting reference IDs from the resource (avoids redundant deep walk).

    When *precomputed_mapping* is ``None`` (N=1 fast path), calls
    :func:`run_gpas_batch` which makes its own ``pseudonymize_batch`` call.
    """
    # Separate depseudo work items from pseudo work items
    depseudo_work = [w for w in gpas_work if w.rule.get("action") == "gpas_depseudonymize"]
    pseudo_work = [w for w in gpas_work if w.rule.get("action") != "gpas_depseudonymize"] if depseudo_work else gpas_work

    if precomputed_mapping is not None:
        batch_mapping = write_back_gpas_batch(
            resource, pseudo_work, precomputed_mapping, processing_mode
        )
    else:
        batch_mapping = run_gpas_batch(
            resource, pseudo_work, processing_mode, pseudonymizer
        )

    # Batch de-pseudonymization (separate from pseudonymization)
    if depseudo_work:
        run_gpas_depseudo_batch(resource, depseudo_work, processing_mode)

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

    # Merged single-walk path: when we have a pre-computed ref mapping
    # (N>1 batch) we can do both ref pseudonymization + text-ID replacement
    # in one tree walk instead of two.
    if precomputed_ref_mapping is not None and do_refs:
        _post_process_resource(
            resource,
            ref_mapping=precomputed_ref_mapping,
            id_map=id_text_map,
            automaton=prebuilt_text_id_automaton,
            compiled=precompiled_text_id_regex,
        )
    else:
        # N=1 path or no precomputed mapping — separate walks
        if do_refs:
            gpas_params = _extract_gpas_params(settings)
            if gpas_params:
                _deep_rewrite_references_gpas(resource, gpas_params, pseudonymizer)

        if id_text_map:
            _rewrite_text_ids(
                resource,
                id_text_map,
                compiled=precompiled_text_id_regex,
                automaton=prebuilt_text_id_automaton,
            )

    # Attach transformation manifest (when enabled or forced for scoring)
    if (attach_manifest or _MANIFEST_ENABLED) and manifest_entries:
        _attach_manifest(resource, manifest_entries)

    return resource


# ---------------------------------------------------------------------------
# Batch processing (unified)
# ---------------------------------------------------------------------------


def _pass1_single(resource, settings, processing_mode, collect_refs=False):
    """Run Pass 1 for a single resource — suitable for thread pool dispatch.

    In ``skip`` mode, lazily snapshots the resource before mutation so that a
    partially de-identified resource is never emitted (PHI leak prevention).
    The snapshot is only taken when the resource has matching rules (i.e.,
    mutations will actually occur), avoiding JSON allocation for the common
    success path and for resources with no matching rules.

    When *collect_refs* is True, collects reference IDs from the resource
    **before** Pass 1 runs so that original (pre-pseudonymisation) IDs are
    captured.  Reference collection in the parallel thread pool also avoids
    a separate serial pass after all Pass 1 work completes.
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
        # Collect reference IDs BEFORE Pass 1 mutates the resource so that
        # outbound references capture original (pre-pseudonymisation) IDs.
        # Collecting after dispatch_pass1 would yield already-pseudonymised
        # IDs (e.g. pat_xxx instead of the original UUID), producing useless
        # pseudonym→pseudonym entries in shared_mapping.
        # ref_type_map: {bare_id: resource_type} — enables per-type gPAS domain routing.
        ref_type_map: "dict[str, str] | None" = None
        if collect_refs:
            _ref_ids: set[str] = set()
            ref_type_map = {}
            _collect_reference_ids(resource, _ref_ids, ref_types=ref_type_map)
        gpas_work, nlp_work = dispatch_pass1(
            resource, rules, settings, manifest_entries, processing_mode
        )
        return resource, gpas_work, nlp_work, manifest_entries, ref_type_map
    except Exception:
        if snapshot is not None:
            # Restore original resource to prevent emitting partial de-identification
            resource.clear()
            resource.update(_json_loads(snapshot))
        raise


def process_data_batch(
    resources: list[dict],
    settings,
    pseudonymizer=None,
    attach_manifest: bool = False,
    _exclude_cached: set[str] | None = None,
    _seen_accumulator: set[str] | None = None,
    _return_manifest: bool = False,
) -> "list[dict] | tuple[list[dict], list[list[dict]]]":
    """De-identify / pseudonymize a batch of FHIR resources with cross-resource
    gPAS batching.

    For *N=1*, skips the cross-resource pre-fetch and goes straight to
    per-resource processing — identical performance to the old single-resource
    path.

    For *N>1*:
      1. Pass 1 on ALL resources (FHIRPath + non-gPAS actions, collect BatchWork)
      2. ONE gPAS HTTP call for all deduped values across all resources
      3. Pass 2 on each resource (cache hits, zero additional HTTP calls)
      4. Post-processing on each resource

    Args:
        resources:        List of FHIR resource dicts (not Bundles — those are
                          handled by ``_process_bundle``).
        settings:         Loaded :class:`~pipeline.config.Settings` instance.
        pseudonymizer:    Optional :class:`~pipeline.ports.PseudonymizerPort`.
        _return_manifest: When True, return ``(results, manifest_entries_list)``
                          instead of just ``results``.  The caller can pass the
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

    processing_mode = _processing_errors_mode(settings)

    # -- N=1 fast path -------------------------------------------------------
    # Collect reference IDs BEFORE Pass 1 so original UUIDs are captured, then
    # batch all gPAS IDs (values + references) in ONE HTTP round-trip.
    if len(resources) == 1:
        resource = resources[0]
        try:
            rules = _get_rules_for_resource(resource, settings)
            manifest_entries: list[dict] = []
            gpas_params = _extract_gpas_params(settings)
            do_refs = getattr(settings, "rewrite_references", False)

            # Collect reference IDs BEFORE Pass 1 mutates the resource so that
            # outbound references capture original (pre-pseudonymisation) IDs.
            # Collecting after dispatch_pass1 would yield already-pseudonymised
            # IDs, producing useless pseudonym→pseudonym entries in combined_mapping.
            ref_ids: set[str] = set()
            ref_type_map_n1: dict[str, str] = {}
            if do_refs and gpas_params:
                _collect_reference_ids(resource, ref_ids, ref_types=ref_type_map_n1)

            gpas_work, nlp_work = dispatch_pass1(
                resource, rules, settings, manifest_entries, processing_mode
            )

            # Pass 1.5: NLP batch (single resource)
            if nlp_work:
                from pipeline.nlp_orchestrator import run_nlp_batch_single

                run_nlp_batch_single(
                    resource, nlp_work, manifest_entries, processing_mode
                )

            # Build combined unique ID list: gpas_work values first, then any
            # reference IDs not already present (dedup via set membership).
            value_id_set = {item.serialized_value for item in gpas_work}
            combined_ids = list(value_id_set) + [
                r for r in ref_ids if r not in value_id_set
            ]

            # Group the combined IDs by their resolved gpas_domain and call gPAS
            # once per distinct domain.  This ensures reference IDs (which use
            # gpas_params / the default domain) and value IDs (which may have been
            # routed to per-resource-type leaf domains by domain_map) each land in
            # the correct gPAS pseudonym namespace.
            combined_mapping: dict = {}
            if combined_ids and (gpas_work or gpas_params):
                # Build a per-domain bucket: value IDs inherit their BatchWork domain;
                # reference IDs that are NOT in gpas_work use the default domain.
                _domain_map_n1 = getattr(settings, "domain_map", None)
                value_id_to_params: dict[str, dict] = {}
                for item in gpas_work:
                    value_id_to_params[item.serialized_value] = item.params
                domain_to_params_n1: dict[str, dict] = {}
                domain_to_values_n1: dict[str, list[str]] = {}
                for vid in combined_ids:
                    # Cross-chunk dedup: skip values already pseudonymized in a
                    # previous chunk (their mappings are in the cache).
                    if _exclude_cached and vid in _exclude_cached:
                        continue
                    params_for_id = value_id_to_params.get(vid)
                    if params_for_id is None:
                        # Reference ID: route by resource type if domain_map is configured
                        rtype = ref_type_map_n1.get(vid)
                        if rtype and _domain_map_n1 and rtype in _domain_map_n1:
                            params_for_id = dict(gpas_params)
                            params_for_id["gpas_domain"] = _domain_map_n1[rtype]
                        else:
                            params_for_id = gpas_params
                    if params_for_id is None:
                        continue
                    d = params_for_id.get("gpas_domain", "")
                    if d not in domain_to_params_n1:
                        domain_to_params_n1[d] = params_for_id
                        domain_to_values_n1[d] = []
                    domain_to_values_n1[d].append(vid)
                for d, vals in domain_to_values_n1.items():
                    try:
                        partial = pseudonymizer.pseudonymize_batch(
                            list(dict.fromkeys(vals)), domain_to_params_n1[d]
                        )
                        combined_mapping.update(partial)
                    except GpasUnavailableError:
                        raise
                    except Exception:
                        if processing_mode != "skip":
                            raise
                # Accumulate newly-pseudonymized values for the next chunk's exclusion.
                if _seen_accumulator is not None:
                    _seen_accumulator.update(combined_mapping.keys())

            precomputed_ref_mapping = combined_mapping if ref_ids else None

            result = _finalize_resource(
                resource,
                settings,
                pseudonymizer,
                gpas_work,
                manifest_entries,
                processing_mode,
                precomputed_mapping=combined_mapping,
                precomputed_ref_mapping=precomputed_ref_mapping,
                attach_manifest=attach_manifest,
            )
            if _return_manifest:
                return [result], [manifest_entries]
            return [result]
        except Exception as exc:
            rtype = (
                resource.get("resourceType", "Unknown")
                if isinstance(resource, dict)
                else "Unknown"
            )
            audit_log.error("batch_single_error resource_type=%s: %s", rtype, exc)
            if processing_mode != "skip":
                raise
            err = [{"error": "processing error", "resourceType": rtype}]
            if _return_manifest:
                return err, [[]]
            return err

    # -- N>1 batch path: cross-resource gPAS pre-fetch ----------------------
    gpas_params = _extract_gpas_params(settings)
    _need_refs = bool(getattr(settings, "rewrite_references", False) and gpas_params)

    parsed: list[dict | None] = []
    all_gpas_works: list[list] = []
    all_nlp_works: list[list] = []
    all_manifest_entries: list[list] = []
    _all_ref_type_map: dict[str, str] = {}  # ref_id → resource_type for per-domain routing

    # Step 1: Pass 1 on all resources (optionally parallel).
    # Collects reference IDs during the same call so that reference collection
    # benefits from thread pool parallelism instead of running serially after.
    if _PARALLEL_WORKERS > 0:
        pool = get_executor()
        futures = []
        _parallel_fell_back = False
        for resource in resources:
            try:
                futures.append(
                    pool.submit(
                        _pass1_single, resource, settings, processing_mode,
                        collect_refs=_need_refs,
                    )
                )
            except TimeoutError:
                # Pool saturated — fall back to sequential for remaining
                audit_log.warning(
                    "pass1_submit_timeout: thread pool saturated after %d/%d resources, "
                    "falling back to sequential",
                    len(futures), len(resources),
                )
                _parallel_fell_back = True
                break
        # Collect results from successfully submitted futures
        for idx, future in enumerate(futures):
            try:
                resource, gpas_work, nlp_work, manifest_entries_, ref_type_map_ = future.result()
                parsed.append(resource)
                all_gpas_works.append(gpas_work)
                all_nlp_works.append(nlp_work)
                all_manifest_entries.append(manifest_entries_)
                if ref_type_map_:
                    _all_ref_type_map.update(ref_type_map_)
            except Exception as exc:
                audit_log.error("batch_pass1_error: %s", exc)
                if processing_mode != "skip":
                    raise
                parsed.append(None)
                all_gpas_works.append([])
                all_nlp_works.append([])
                all_manifest_entries.append([])
        # Process remaining resources sequentially if pool submit timed out
        if _parallel_fell_back:
            for resource in resources[len(futures):]:
                try:
                    resource, gpas_work, nlp_work, manifest_entries_, ref_type_map_ = _pass1_single(
                        resource, settings, processing_mode,
                        collect_refs=_need_refs,
                    )
                except Exception as exc:
                    audit_log.error("batch_pass1_error (sequential fallback): %s", exc)
                    if processing_mode != "skip":
                        raise
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
                resource, gpas_work, nlp_work, manifest_entries_, ref_type_map_ = _pass1_single(
                    resource, settings, processing_mode,
                    collect_refs=_need_refs,
                )
            except Exception as exc:
                rtype = (
                    resource.get("resourceType", "Unknown")
                    if isinstance(resource, dict)
                    else "Unknown"
                )
                audit_log.error("batch_pass1_error resource_type=%s: %s", rtype, exc)
                if processing_mode != "skip":
                    raise
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

    # Step 1.5: NLP batch across all resources (detect → replace).
    if any(nw for nw in all_nlp_works):
        from pipeline.nlp_orchestrator import run_nlp_batch_for_batch

        run_nlp_batch_for_batch(
            parsed, all_nlp_works, all_manifest_entries, processing_mode
        )

    # Step 2: ONE gPAS HTTP call for all unique values across all resources.
    # Reference IDs were already collected during _pass1_single (above) so they
    # are included in the same batch — avoids a second HTTP round-trip and
    # benefits from parallel thread pool execution.

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

    shared_mapping = run_gpas_batch_for_batch(
        all_gpas_works,
        processing_mode,
        pseudonymizer,
        gpas_params,
        extra_values_by_domain=_extra_values_by_domain,
        exclude_cached=_exclude_cached,
    )
    if _seen_accumulator is not None and shared_mapping:
        _seen_accumulator.update(shared_mapping.keys())

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

    # Step 3+4: Per-resource finalize (write-back from shared mapping, zero HTTP)
    results: list[dict | None] = [None] * len(parsed)
    n_resources = len(parsed)

    if n_resources > 4:
        # Parallel finalization: each resource is independent (shared_mapping is read-only)
        pool = get_executor()
        futures = {}
        for i, (resource, gpas_work, manifest_entries_) in enumerate(
            zip(parsed, all_gpas_works, all_manifest_entries)
        ):
            if resource is None:
                results[i] = {"error": "pass1 error", "resourceType": "Unknown"}
                continue
            try:
                fut = pool.submit(
                    _finalize_resource,
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
                    "finalize_submit_timeout: thread pool saturated at resource %d/%d, "
                    "falling back to sequential",
                    i, n_resources,
                )
                # Finalize this and remaining resources sequentially after draining futures
                _finalize_sequential_start = i
                break
        else:
            _finalize_sequential_start = None

        from concurrent.futures import as_completed

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
                audit_log.error("batch_process_error resource_type=%s: %s", rtype, exc)
                if processing_mode != "skip":
                    raise
                results[i] = {"error": "processing error", "resourceType": rtype}

        # Finalize remaining resources sequentially if pool submit timed out
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
                    results[i] = {"error": "pass1 error", "resourceType": "Unknown"}
                    continue
                try:
                    results[i] = _finalize_resource(
                        resource, settings, pseudonymizer, gpas_work, manifest_entries_,
                        processing_mode,
                        precomputed_mapping=shared_mapping,
                        precompiled_text_id_regex=_batch_text_id_regex,
                        precomputed_ref_mapping=_batch_ref_mapping,
                        prebuilt_text_id_automaton=_batch_text_id_automaton,
                        attach_manifest=attach_manifest,
                    )
                except Exception as exc:
                    rtype = resource.get("resourceType", "Unknown") if isinstance(resource, dict) else "Unknown"
                    audit_log.error("batch_process_error resource_type=%s: %s", rtype, exc)
                    if processing_mode != "skip":
                        raise
                    results[i] = {"error": "processing error", "resourceType": rtype}

        # Free intermediate data after all futures complete
        for i in range(n_resources):
            parsed[i] = None
            all_gpas_works[i] = []
            all_nlp_works[i] = []
            if not _return_manifest:
                all_manifest_entries[i] = []
    else:
        # Small batch — sequential (no thread pool overhead)
        for i, (resource, gpas_work, manifest_entries_) in enumerate(
            zip(parsed, all_gpas_works, all_manifest_entries)
        ):
            if resource is None:
                results[i] = {"error": "pass1 error", "resourceType": "Unknown"}
            else:
                try:
                    result = _finalize_resource(
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
                    audit_log.error("batch_process_error resource_type=%s: %s", rtype, exc)
                    if processing_mode != "skip":
                        raise
                    results[i] = {"error": "processing error", "resourceType": rtype}
            # Free intermediate data for this resource to reduce peak memory
            parsed[i] = None
            all_gpas_works[i] = []
            all_nlp_works[i] = []
            if not _return_manifest:
                all_manifest_entries[i] = []

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

    # Batch-process all inner resources in a single call so gPAS dedup spans
    # the entire Bundle (not per-chunk).  process_data_batch already handles
    # memory-bounded chunking internally via _BATCH_SIZE for the gPAS HTTP call.
    if inner_resources:
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
        if bare_id_map or do_text_ids:
            automaton, compiled = (None, None)
            if do_text_ids:
                automaton, compiled = _build_text_id_matcher(id_text_map)
                audit_log.debug("rewriting_text_ids count=%d", len(id_text_map))
            _post_process_resource(
                resource,
                ref_mapping=bare_id_map or None,
                id_map=id_text_map if do_text_ids else None,
                automaton=automaton,
                compiled=compiled,
            )
    elif getattr(settings, "rewrite_text_ids", False):
        # No ref_map but text_ids requested — nothing to rewrite
        pass

    return resource


# ---------------------------------------------------------------------------
# Public entry point (backward-compatible)
# ---------------------------------------------------------------------------


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
        return process_data_batch(
            resource, settings, pseudonymizer, attach_manifest=attach_manifest
        )
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

    Yields one processed resource dict at a time.  Memory usage is bounded by
    ``chunk_size × avg_resource_size`` instead of growing with the total input.

    Cross-chunk gPAS dedup uses an explicit rolling ``seen_values`` set: original
    values pseudonymized in chunk N are excluded from the gPAS HTTP call for chunk
    N+1, bypassing even the L1 cache lookup.  The L1 cache remains the primary
    guard; this set removes values from the dedup candidates before batching.

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

    seen_values = _CappedSet()
    chunk: list[dict] = []
    for resource in resources_iter:
        chunk.append(resource)
        if len(chunk) >= chunk_size:
            yield from process_data_batch(
                chunk, settings, pseudonymizer,
                attach_manifest=attach_manifest,
                _exclude_cached=seen_values,
                _seen_accumulator=seen_values,
            )
            chunk = []
    if chunk:
        yield from process_data_batch(
            chunk, settings, pseudonymizer,
            attach_manifest=attach_manifest,
            _exclude_cached=seen_values,
            _seen_accumulator=seen_values,
        )
