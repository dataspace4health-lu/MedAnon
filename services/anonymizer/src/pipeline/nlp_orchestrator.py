"""Pass 1.5: batch NLP detection and replacement.

Collects texts from all deferred PHIDetectionTask items, runs entity detection
in a single batch (one HTTP call for remote, or cache-prewarming for local),
then applies replacements per-resource with proper token_state isolation.

Mirrors the gPAS batch pattern in ``gpas_orchestrator.py``.
"""

from __future__ import annotations

import base64
import logging
import os

from utils.fhirpath import find_nodes  # noqa: F401  (re-exported for test patch targets)
from pipeline.manifest import _MANIFEST_ENABLED
from pipeline.action_dispatcher import PHIDetectionTask  # noqa: F401

# Field & attachment extraction lives in pipeline.nlp_extract; re-imported here
# so module-level references (and tests that patch ``nlp_orchestrator._FieldText``
# etc.) keep resolving.
from pipeline.nlp_extract import (
    _MIME_INDICATOR_FIELDS,  # noqa: F401
    _TEXT_MIME_TYPES,  # noqa: F401
    _HEURISTIC_SENTINEL,  # noqa: F401
    _FieldText,
    _discover_text_attachments,
    _extract_fields,
)

_log = logging.getLogger("medanon.nlp_batch")


def _attachment_scan_enabled() -> bool:
    """Whether the heuristic Base64/attachment scanner should run.

    OFF by default (MEDANON_ATTACHMENT_SCAN). Read lazily so a process that sets
    the env var sees it without depending on processor import order, and so tests
    can toggle it via monkeypatched os.environ.
    """
    return os.environ.get("MEDANON_ATTACHMENT_SCAN", "false").strip().lower() in (
        "true",
        "1",
        "yes",
        "on",
    )


# ---------------------------------------------------------------------------
# Replacement helpers
# ---------------------------------------------------------------------------


def _resolve_nlp_params(work_item: PHIDetectionTask):
    """Extract NLP parameters from a work item."""
    from integrations.nlp.utils import _resolve_entities

    params = work_item.params
    entities = _resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    return entities, threshold, language


def _apply_nlp_scrub(
    text: str, adapter, entities, threshold, language, mode, token_state
):
    """Apply nlp_scrub replacement: detect + uniform tokenize/redact."""
    return adapter.analyze_and_replace(
        text, entities, threshold, language, mode, token_state
    )


def _apply_nlp_detect_act(
    text: str, adapter, entities, threshold, language, params, token_state
):
    """Apply nlp_detect_act replacement: detect + entity-specific actions.

    Spans are replaced right-to-left (descending by start position) so that
    each replacement only shifts characters to the right of all remaining spans,
    keeping their original (start, end) positions valid throughout the loop.
    """
    from pipeline.deidentify import (
        _DEFAULT_ENTITY_ACTIONS,
        _merge_overlapping_spans,
        _replace_span,
    )

    if not text or not text.strip():
        return text, False

    hits = adapter.detect(text, entities, threshold, language)
    if not hits:
        return text, False

    # Resolve overlapping detections (e.g. ACCOUNT_LABEL covering an inner
    # US_DRIVER_LICENSE span) into a non-overlapping right-to-left list so
    # the replacement loop cannot leave fragmentary tokens behind.
    hits = _merge_overlapping_spans(hits)

    entity_actions = {**_DEFAULT_ENTITY_ACTIONS, **(params.get("entity_actions") or {})}
    default_action = entity_actions.get("default", "tokenize")

    for start, end, entity_type in hits:
        if not text[start:end].strip():
            continue
        ea = entity_actions.get(entity_type, default_action)
        text = _replace_span(text, start, end, entity_type, ea, token_state)

    return text, True


def _apply_replacement(field: _FieldText, adapter, token_state):
    """Apply NLP replacement to a single text field. Returns (result_text, changed)."""
    entities, threshold, language = field.nlp_params
    text = field.text
    action_type = field.work_item.action_type

    if action_type == "nlp_detect_act":
        return _apply_nlp_detect_act(
            text,
            adapter,
            entities,
            threshold,
            language,
            field.work_item.params,
            token_state,
        )
    else:
        # nlp_scrub / nlp_detect
        mode = str(field.work_item.params.get("mode", "tokenize"))
        result = _apply_nlp_scrub(
            text, adapter, entities, threshold, language, mode, token_state
        )
        return result, result != text


def _apply_replacement_xhtml(field: _FieldText, adapter, token_state):
    """Apply NLP replacement to an XHTML text field. Returns (result_text, changed)."""
    from integrations.nlp.utils import _scrub_xhtml_text_nodes

    entities, threshold, language = field.nlp_params
    action_type = field.work_item.action_type

    if action_type == "nlp_detect_act":
        from pipeline.deidentify import (
            _DEFAULT_ENTITY_ACTIONS,
            _merge_overlapping_spans,
            _replace_span,
        )

        entity_actions = {
            **_DEFAULT_ENTITY_ACTIONS,
            **(field.work_item.params.get("entity_actions") or {}),
        }
        default_action = entity_actions.get("default", "tokenize")

        def scrub_fn(text):
            if not text or not text.strip():
                return text
            hits = adapter.detect(text, entities, threshold, language)
            if not hits:
                return text
            # Resolve overlapping spans before right-to-left replacement
            # to avoid fragmentary tokens like "[ACCOUNT_LABEL]ER_LICENSE]".
            sorted_hits = _merge_overlapping_spans(hits)
            for start, end, entity_type in sorted_hits:
                if not text[start:end].strip():
                    continue
                ea = entity_actions.get(entity_type, default_action)
                text = _replace_span(text, start, end, entity_type, ea, token_state)
            return text
    else:
        mode = str(field.work_item.params.get("mode", "tokenize"))

        def scrub_fn(text):
            return adapter.analyze_and_replace(
                text, entities, threshold, language, mode, token_state
            )

    result = _scrub_xhtml_text_nodes(field.text, scrub_fn)
    return result, result != field.text


# ---------------------------------------------------------------------------
# Batch NLP execution (cross-resource)
# ---------------------------------------------------------------------------


def _batch_detect_prewarm(
    adapter, unique_texts: list[str], entities, threshold, language
):
    """Pre-warm the detection cache by running batch detection on all unique texts.

    After this call, subsequent per-text ``adapter.detect()`` or
    ``adapter.analyze_and_replace()`` calls will be cache hits (local) or
    avoid redundant HTTP calls (remote).
    """
    if hasattr(adapter, "detect_batch"):
        try:
            adapter.detect_batch(unique_texts, entities, threshold, language)
            return
        except Exception:
            _log.warning("detect_batch_failed — falling back to sequential pre-warm")

    # Fallback: sequential detection (still pre-warms the per-process cache)
    failed = 0
    for text in unique_texts:
        try:
            adapter.detect(text, entities, threshold, language)
        except Exception:
            failed += 1
    if failed:
        _log.warning(
            "nlp_sequential_prewarm: %d/%d texts failed detection — "
            "affected fields will be redacted during replacement",
            failed,
            len(unique_texts),
        )


def detect_phi_batch(
    resources: list[dict | None],
    all_nlp_works: list[list[PHIDetectionTask]],
    all_manifest_entries: list[list[dict]],
    processing_mode: str,
) -> None:
    """Batch NLP detection across all resources, then apply results per-resource.

    Phase A: Extract texts from all NLP work items across all resources.
    Phase B: Deduplicate and batch-detect all unique texts (one HTTP call or
             cache-prewarming for local Presidio).
    Phase C: Per-resource replacement with proper token_state isolation.
    """
    from pipeline.deidentify import _get_nlp_adapter, _NLP_FAIL_MODE
    from pipeline.exceptions import NlpUnavailableError
    from actions.redact import redact_by_path

    adapter = _get_nlp_adapter()
    if adapter is None:
        # Fallback: redact all NLP-targeted fields
        for i, nlp_works in enumerate(all_nlp_works):
            if not nlp_works or resources[i] is None:
                continue
            for work_item in nlp_works:
                if _NLP_FAIL_MODE == "raise":
                    raise NlpUnavailableError(
                        "NLP adapter unavailable — cannot process batch"
                    )
                _log.error(
                    "nlp_unavailable — redacting %s", work_item.element.get("path", "?")
                )
                redact_by_path(resources[i], work_item.element, {})
                if _MANIFEST_ENABLED:
                    all_manifest_entries[i].append(
                        {
                            "rule": work_item.rule.get("name", work_item.rule["match"]),
                            "action": "redact",
                            "path": work_item.element.get("path", "?"),
                        }
                    )
        return

    # Phase A: Extract all text fields from config-rule PHIDetectionTask items.
    all_fields: list[_FieldText] = []
    for i, (resource, nlp_works) in enumerate(zip(resources, all_nlp_works)):
        if resource is None or not nlp_works:
            continue
        for work_item in nlp_works:
            fields = _extract_fields(resource, work_item, i)
            if not fields:
                # Path navigation failed or field empty — redact as fallback
                redact_by_path(resource, work_item.element, {})
                if _MANIFEST_ENABLED:
                    all_manifest_entries[i].append(
                        {
                            "rule": work_item.rule.get("name", work_item.rule["match"]),
                            "action": "redact",
                            "path": work_item.element.get("path", "?"),
                        }
                    )
            all_fields.extend(fields)

    # Phase A1: Heuristic attachment scan — find any {contentType, data} pair
    # anywhere in each resource, regardless of resource type or nesting depth.
    # Runs after Phase A so that config-rule fields are already in the claimed set;
    # this prevents double-processing nodes that are covered by explicit rules.
    # Pre-filter: only recurse into resources that have at least one of the three
    # attachment signal tokens.  False positives are safe (they walk and find
    # nothing); false negatives are impossible for the three patterns.
    #
    # Gated on MEDANON_ATTACHMENT_SCAN (default OFF): without this guard the scan
    # would scrub attachment data even when no config rule targets it, mutating
    # fields the user never asked to transform. Read lazily so the env var is
    # respected per process without import-order coupling.
    if _attachment_scan_enabled():
        _ATTACH_SIGNALS = ("\"data\"", "data:", "Base64Binary")
        claimed: set[tuple] = {(id(f.owner), f.key) for f in all_fields}
        heuristic_fields: list[_FieldText] = []
        for i, resource in enumerate(resources):
            if resource is None:
                continue
            import json as _json
            _serialized = _json.dumps(resource, separators=(",", ":"))
            if not any(sig in _serialized for sig in _ATTACH_SIGNALS):
                continue
            _discover_text_attachments(resource, i, claimed, heuristic_fields)
        if heuristic_fields:
            _log.debug(
                "heuristic_attachment_scan: found %d text attachment(s)",
                len(heuristic_fields),
            )
        all_fields.extend(heuristic_fields)

    if not all_fields:
        return

    # Pre-compute NLP params once per unique PHIDetectionTask instance.
    # _resolve_entities() creates a fresh list copy each call; doing it once
    # per work_item (vs once per field) avoids O(fields) list allocations.
    wi_params: dict[int, tuple] = {}
    for f in all_fields:
        wi_id = id(f.work_item)
        if wi_id not in wi_params:
            wi_params[wi_id] = _resolve_nlp_params(f.work_item)
        f.nlp_params = wi_params[wi_id]

    # Phase B: Batch detection — pre-warm cache for all unique texts
    # Group by (entities, threshold, language) so mixed-param configs get
    # correct detection results instead of using first-field params for all.
    param_groups: dict[tuple, set[str]] = {}
    for f in all_fields:
        if not f.text or not f.text.strip():
            continue
        entities, threshold, language = f.nlp_params
        key = (tuple(entities), threshold, language)
        if key not in param_groups:
            param_groups[key] = set()
        param_groups[key].add(f.text)

    for (entities_tuple, threshold, language), texts in param_groups.items():
        _batch_detect_prewarm(
            adapter, list(texts), list(entities_tuple), threshold, language
        )

    # Phase C: Per-resource replacement with token_state scoping.
    #
    # mapping_scope controls token consistency across resources:
    #   resource   (default) — fresh token_state per resource; tokens are
    #                          independent (e.g. [[PERSON_1]] in one resource
    #                          has no relationship to [[PERSON_1]] in another).
    #   bundle               — one shared token_state for the entire batch so
    #                          the same surface form maps to the same surrogate
    #                          across all resources (e.g. a patient name in
    #                          Patient and in DocumentReference share [[PERSON_1]]).
    #                          Forces sequential Phase-C execution because the
    #                          token map is shared mutable state.
    #   global_run           — uses the module-level singleton from scrub_text.py;
    #                          tokens are stable across all batches in a CLI run.
    #
    # Read scope from any NLP work-item params (all items in one batch must use
    # the same scope; first non-heuristic item wins, default 'resource').
    _mapping_scope = "resource"
    for _f in all_fields:
        if _f.work_item is not _HEURISTIC_SENTINEL:
            _mapping_scope = str(_f.work_item.params.get("mapping_scope", "resource"))
            break

    # Build the shared token_state for non-resource scopes.
    if _mapping_scope == "global_run":
        from actions.scrub_text import (
            _GLOBAL_TOKEN_STATE,
            _GLOBAL_TOKEN_LOCK as _G_LOCK,
        )

        _shared_token_state: dict | None = _GLOBAL_TOKEN_STATE
        _shared_token_lock = _G_LOCK
    elif _mapping_scope == "bundle":
        _shared_token_state = {"next": {}, "map": {}, "reverse": {}}
        _shared_token_lock = None  # only one thread will use it (sequential phase-C)
    else:
        _shared_token_state = None  # per-resource fresh state
        _shared_token_lock = None

    # Group fields by resource index for per-resource token_state.
    fields_by_resource: dict[int, list[_FieldText]] = {}
    for f in all_fields:
        fields_by_resource.setdefault(f.resource_idx, []).append(f)

    # Track which work items had changes (for manifest).
    # Heuristic fields all share _HEURISTIC_SENTINEL so they are tracked
    # separately by (resource_idx, path_hint) to emit per-field manifest entries.
    work_item_changed: dict[
        int, dict[int, bool]
    ] = {}  # resource_idx -> {work_item_id -> changed}
    heuristic_changed: dict[
        int, list[str]
    ] = {}  # resource_idx -> [path_hints that changed]
    # Fields where NLP failed and a blanket redact fired — Phase D records
    # these as action=redact/reason=nlp_fallback instead of the original
    # action name, so the manifest never claims NLP succeeded when it didn't.
    work_item_fallback: dict[int, set[int]] = {}  # resource_idx -> {work_item_id}
    heuristic_fallback: dict[int, set[str]] = {}  # resource_idx -> {path_hint}

    def _run_resource(
        res_idx: int,
        fields: list[_FieldText],
        token_state: dict | None = None,
        token_lock=None,
    ) -> tuple[int, dict[int, bool], list[str], set[int], set[str]]:
        """Apply NLP replacements for one resource.

        Thread-safe for ``resource`` scope (each call gets its own ``token_state``
        and writes only to its own resource dict entries).

        For ``bundle`` / ``global_run`` scopes the caller passes a shared
        ``token_state``; Phase-C is run sequentially so the lock is None.
        For ``global_run`` the lock is non-None and held around each
        ``_tokenize`` call (delegated through ``_apply_replacement*``).

        Returns ``(res_idx, resource_changed, heuristic_paths, fallback_wi,
        fallback_paths)`` — the last two identify fields where NLP failed and a
        blanket redact fired, so Phase D records the *actual* outcome instead
        of claiming NLP processing succeeded.
        """
        if token_state is None:
            token_state = {"next": {}, "map": {}, "reverse": {}}

        resource_changed: dict[int, bool] = {}
        heuristic_paths: list[str] = []
        fallback_wi: set[int] = set()
        fallback_paths: set[str] = set()

        for field in fields:
            wi_id = id(field.work_item)
            try:
                if field.is_xhtml:
                    result, changed = _apply_replacement_xhtml(
                        field, adapter, token_state
                    )
                else:
                    result, changed = _apply_replacement(field, adapter, token_state)

                if changed:
                    if field.base64_encoded:
                        result = base64.b64encode(result.encode("utf-8")).decode(
                            "ascii"
                        )
                    if field.data_uri_prefix:
                        result = field.data_uri_prefix + result
                    if token_lock is not None:
                        with token_lock:
                            field.owner[field.key] = result
                    else:
                        field.owner[field.key] = result
                    if field.work_item is _HEURISTIC_SENTINEL:
                        heuristic_paths.append(field.path_hint)
                    else:
                        resource_changed[wi_id] = True
                elif (
                    wi_id not in resource_changed
                    and field.work_item is not _HEURISTIC_SENTINEL
                ):
                    resource_changed[wi_id] = False
            except Exception as exc:
                from pipeline.deidentify import _is_bug

                if _is_bug(exc):
                    # A programming defect (e.g. AttributeError) must not be
                    # masked as a redaction — re-raise so it is observable.
                    raise
                _log.error(
                    "nlp_batch_replace_failed res=%d key=%s — redacting",
                    res_idx,
                    field.key,
                )
                field.owner[field.key] = "[REDACTED]"
                try:
                    from utils.metrics import ACTION_FALLBACK

                    _fb_action = (
                        "nlp_attachment_scan"
                        if field.work_item is _HEURISTIC_SENTINEL
                        else str(field.work_item.action_type)
                    )
                    ACTION_FALLBACK.labels(
                        action=_fb_action, reason=type(exc).__name__
                    ).inc()
                except Exception:  # noqa: BLE001 — metrics must never break the pipeline
                    pass
                if field.work_item is _HEURISTIC_SENTINEL:
                    heuristic_paths.append(field.path_hint)
                    fallback_paths.add(field.path_hint)
                else:
                    resource_changed[wi_id] = True
                    fallback_wi.add(wi_id)

        return res_idx, resource_changed, heuristic_paths, fallback_wi, fallback_paths

    # bundle / global_run scopes require sequential execution because the token
    # map is shared mutable state.  Only the default ``resource`` scope uses
    # the thread pool.
    _use_parallel = (_shared_token_state is None) and len(fields_by_resource) > 1

    if _use_parallel:
        # Parallel path: each resource is independent (separate token_state +
        # writes to its own dict entries).  Mirrors the parallel finalization
        # pattern in pipeline/processor.py.
        from utils.thread_pool import get_executor
        from concurrent.futures import as_completed

        pool = get_executor()
        futures: dict = {}
        sequential_fallback: list[tuple[int, list[_FieldText]]] = []

        for res_idx, fields in fields_by_resource.items():
            try:
                futures[pool.submit(_run_resource, res_idx, fields)] = res_idx
            except TimeoutError:
                _log.warning(
                    "nlp_phase_c_submit_timeout: thread pool saturated at res=%d, "
                    "falling back to sequential",
                    res_idx,
                )
                sequential_fallback.append((res_idx, fields))

        for fut in as_completed(futures):
            try:
                idx, rc, hp, fwi, fpaths = fut.result()
                work_item_changed[idx] = rc
                if hp:
                    heuristic_changed[idx] = hp
                if fwi:
                    work_item_fallback[idx] = fwi
                if fpaths:
                    heuristic_fallback[idx] = fpaths
            except Exception:
                _log.error("nlp_phase_c_error res=%d", futures[fut])

        for res_idx, fields in sequential_fallback:
            idx, rc, hp, fwi, fpaths = _run_resource(res_idx, fields)
            work_item_changed[idx] = rc
            if hp:
                heuristic_changed[idx] = hp
            if fwi:
                work_item_fallback[idx] = fwi
            if fpaths:
                heuristic_fallback[idx] = fpaths
    else:
        # Sequential path: single resource, or shared token scope (bundle/global_run).
        # For global_run, _shared_token_lock guards the token map during tokenize calls;
        # for bundle, the map is already exclusive to this batch (no lock needed).
        for res_idx, fields in fields_by_resource.items():
            idx, rc, hp, fwi, fpaths = _run_resource(
                res_idx,
                fields,
                token_state=_shared_token_state,
                token_lock=_shared_token_lock,
            )
            work_item_changed[idx] = rc
            if hp:
                heuristic_changed[idx] = hp
            if fwi:
                work_item_fallback[idx] = fwi
            if fpaths:
                heuristic_fallback[idx] = fpaths

    # Phase D: Record manifest entries for config-rule NLP work items
    if _MANIFEST_ENABLED:
        for i, nlp_works in enumerate(all_nlp_works):
            if not nlp_works or resources[i] is None:
                continue
            res_changes = work_item_changed.get(i, {})
            res_fallback = work_item_fallback.get(i, set())
            for work_item in nlp_works:
                wi_id = id(work_item)
                changed = res_changes.get(wi_id, False)
                if wi_id in res_fallback:
                    # NLP failed for (at least one field of) this work item and
                    # a blanket redact fired — record the actual outcome, not
                    # the original action name, so scoring sees the redaction.
                    all_manifest_entries[i].append(
                        {
                            "rule": work_item.rule.get("name", work_item.rule["match"]),
                            "action": "redact",
                            "path": work_item.element.get("path", "?"),
                            "reason": "nlp_fallback",
                        }
                    )
                    continue
                if work_item.action_type == "nlp_detect_act" and not changed:
                    # Text was scanned but no PII found — record as
                    # "scanned" so the scoring system knows this path
                    # was examined (prevents false-positive coverage gaps).
                    all_manifest_entries[i].append(
                        {
                            "rule": work_item.rule.get("name", work_item.rule["match"]),
                            "action": "nlp_detect_act/clean",
                            "path": work_item.element.get("path", "?"),
                        }
                    )
                    continue
                action_name = work_item.action_type
                all_manifest_entries[i].append(
                    {
                        "rule": work_item.rule.get("name", work_item.rule["match"]),
                        "action": action_name,
                        "path": work_item.element.get("path", "?"),
                    }
                )

        # Phase D1: Manifest entries for heuristically discovered attachment fields
        for res_idx, paths in heuristic_changed.items():
            fpaths = heuristic_fallback.get(res_idx, set())
            for path in paths:
                if path in fpaths:
                    entry = {
                        "rule": "auto:attachment_scan",
                        "action": "redact",
                        "path": path,
                        "reason": "nlp_fallback",
                    }
                else:
                    entry = {
                        "rule": "auto:attachment_scan",
                        "action": "nlp_detect_act",
                        "path": path,
                    }
                all_manifest_entries[res_idx].append(entry)


def detect_phi_single(
    resource: dict,
    nlp_work: list[PHIDetectionTask],
    manifest_entries: list[dict],
    processing_mode: str,
) -> None:
    """Run NLP batch for a single resource (N=1 fast path)."""
    detect_phi_batch([resource], [nlp_work], [manifest_entries], processing_mode)


# Backward-compatible aliases.
run_nlp_batch_for_batch = detect_phi_batch
run_nlp_batch_single = detect_phi_single
