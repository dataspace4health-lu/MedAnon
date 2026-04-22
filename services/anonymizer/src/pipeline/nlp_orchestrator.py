"""Pass 1.5: batch NLP detection and replacement.

Collects texts from all deferred NlpWork items, runs entity detection
in a single batch (one HTTP call for remote, or cache-prewarming for local),
then applies replacements per-resource with proper token_state isolation.

Mirrors the gPAS batch pattern in ``gpas_orchestrator.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from utils.fhirpath import find_nodes
from pipeline.manifest import _MANIFEST_ENABLED
from pipeline.action_dispatcher import NlpWork

_log = logging.getLogger("medanon.nlp_batch")


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class _TextField:
    """One text field extracted for NLP processing."""

    text: str
    owner: Any  # containing dict or list (for write-back)
    key: str | int  # dict key or list index
    is_xhtml: bool
    work_item: NlpWork  # back-reference for params/action_type
    resource_idx: int  # index into the parsed resources list
    nlp_params: tuple = ()  # (entities, threshold, language) — populated post-extraction


# ---------------------------------------------------------------------------
# Text extraction (replicates navigation from deidentify.py:145-207)
# ---------------------------------------------------------------------------


def _extract_fields(resource: dict, work_item: NlpWork, resource_idx: int) -> list[_TextField]:
    """Navigate to the matched element and collect all text values for NLP."""
    path = work_item.element.get("path", "")
    parts = path.split(".")
    if len(parts) < 2:
        return []

    key = parts[-1]
    parent_path = parts[1:-1]  # strip the resource-type root segment
    use_html = bool(work_item.params.get("html", False))

    try:
        nodes = find_nodes(resource, parent_path, [])
    except Exception:
        _log.error("nlp_batch_find_nodes_failed path=%s — will redact", path)
        return []

    fields: list[_TextField] = []

    def _collect(node, field):
        if isinstance(node, list):
            for item in node:
                _collect(item, field)
            return
        if not isinstance(node, dict) or field not in node:
            return
        current = node[field]
        if use_html:
            if isinstance(current, dict) and isinstance(current.get("div"), str):
                fields.append(_TextField(
                    text=current["div"], owner=current, key="div",
                    is_xhtml=True, work_item=work_item, resource_idx=resource_idx,
                ))
            elif isinstance(current, str):
                fields.append(_TextField(
                    text=current, owner=node, key=field,
                    is_xhtml=True, work_item=work_item, resource_idx=resource_idx,
                ))
        elif isinstance(current, str):
            fields.append(_TextField(
                text=current, owner=node, key=field,
                is_xhtml=False, work_item=work_item, resource_idx=resource_idx,
            ))
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    fields.append(_TextField(
                        text=v, owner=current, key=i,
                        is_xhtml=False, work_item=work_item, resource_idx=resource_idx,
                    ))

    _collect(nodes, key)
    return fields


# ---------------------------------------------------------------------------
# Replacement helpers
# ---------------------------------------------------------------------------


def _resolve_nlp_params(work_item: NlpWork):
    """Extract NLP parameters from a work item."""
    from integrations.nlp.utils import _resolve_entities

    params = work_item.params
    entities = _resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    return entities, threshold, language


def _apply_nlp_scrub(text: str, adapter, entities, threshold, language, mode, token_state):
    """Apply nlp_scrub replacement: detect + uniform tokenize/redact."""
    return adapter.analyze_and_replace(text, entities, threshold, language, mode, token_state)


def _apply_nlp_detect_act(text: str, adapter, entities, threshold, language, params, token_state):
    """Apply nlp_detect_act replacement: detect + entity-specific actions.

    Spans are replaced right-to-left (descending by start position) so that
    each replacement only shifts characters to the right of all remaining spans,
    keeping their original (start, end) positions valid throughout the loop.
    """
    from pipeline.deidentify import _DEFAULT_ENTITY_ACTIONS, _replace_span

    if not text or not text.strip():
        return text, False

    hits = adapter.detect(text, entities, threshold, language)
    if not hits:
        return text, False

    # Guarantee descending order regardless of adapter implementation.
    # Non-overlapping spans processed right-to-left: replacing span at [s,e)
    # only shifts text at positions >= e, leaving all remaining spans (s' < s)
    # at their original positions in the string.
    hits = sorted(hits, key=lambda h: h[0], reverse=True)

    entity_actions = {**_DEFAULT_ENTITY_ACTIONS, **(params.get("entity_actions") or {})}
    default_action = entity_actions.get("default", "tokenize")

    for start, end, entity_type in hits:
        if not text[start:end].strip():
            continue
        ea = entity_actions.get(entity_type, default_action)
        text = _replace_span(text, start, end, entity_type, ea, token_state)

    return text, True


def _apply_replacement(field: _TextField, adapter, token_state):
    """Apply NLP replacement to a single text field. Returns (result_text, changed)."""
    entities, threshold, language = field.nlp_params
    text = field.text
    action_type = field.work_item.action_type

    if action_type == "nlp_detect_act":
        return _apply_nlp_detect_act(
            text, adapter, entities, threshold, language,
            field.work_item.params, token_state,
        )
    else:
        # nlp_scrub / nlp_detect
        mode = str(field.work_item.params.get("mode", "tokenize"))
        result = _apply_nlp_scrub(text, adapter, entities, threshold, language, mode, token_state)
        return result, result != text


def _apply_replacement_xhtml(field: _TextField, adapter, token_state):
    """Apply NLP replacement to an XHTML text field. Returns (result_text, changed)."""
    from integrations.nlp.utils import _scrub_xhtml_text_nodes

    entities, threshold, language = field.nlp_params
    action_type = field.work_item.action_type

    if action_type == "nlp_detect_act":
        from pipeline.deidentify import _DEFAULT_ENTITY_ACTIONS, _replace_span

        entity_actions = {**_DEFAULT_ENTITY_ACTIONS, **(field.work_item.params.get("entity_actions") or {})}
        default_action = entity_actions.get("default", "tokenize")

        def scrub_fn(text):
            if not text or not text.strip():
                return text
            hits = adapter.detect(text, entities, threshold, language)
            if not hits:
                return text
            # Guarantee descending order for safe right-to-left replacement.
            sorted_hits = sorted(hits, key=lambda h: h[0], reverse=True)
            for start, end, entity_type in sorted_hits:
                if not text[start:end].strip():
                    continue
                ea = entity_actions.get(entity_type, default_action)
                text = _replace_span(text, start, end, entity_type, ea, token_state)
            return text
    else:
        mode = str(field.work_item.params.get("mode", "tokenize"))

        def scrub_fn(text):
            return adapter.analyze_and_replace(text, entities, threshold, language, mode, token_state)

    result = _scrub_xhtml_text_nodes(field.text, scrub_fn)
    return result, result != field.text


# ---------------------------------------------------------------------------
# Batch NLP execution (cross-resource)
# ---------------------------------------------------------------------------


def _batch_detect_prewarm(adapter, unique_texts: list[str], entities, threshold, language):
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


def run_nlp_batch_for_batch(
    resources: list[dict | None],
    all_nlp_works: list[list[NlpWork]],
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
    from actions.redact import redact_by_path

    adapter = _get_nlp_adapter()
    if adapter is None:
        # Fallback: redact all NLP-targeted fields
        for i, nlp_works in enumerate(all_nlp_works):
            if not nlp_works or resources[i] is None:
                continue
            for work_item in nlp_works:
                if _NLP_FAIL_MODE == "raise":
                    raise RuntimeError("NLP adapter unavailable — cannot process batch")
                _log.error("nlp_unavailable — redacting %s", work_item.element.get("path", "?"))
                redact_by_path(resources[i], work_item.element, {})
                if _MANIFEST_ENABLED:
                    all_manifest_entries[i].append({
                        "rule": work_item.rule.get("name", work_item.rule["match"]),
                        "action": "redact",
                        "path": work_item.element.get("path", "?"),
                    })
        return

    # Phase A: Extract all text fields
    all_fields: list[_TextField] = []
    for i, (resource, nlp_works) in enumerate(zip(resources, all_nlp_works)):
        if resource is None or not nlp_works:
            continue
        for work_item in nlp_works:
            fields = _extract_fields(resource, work_item, i)
            if not fields:
                # Path navigation failed or field empty — redact as fallback
                redact_by_path(resource, work_item.element, {})
                if _MANIFEST_ENABLED:
                    all_manifest_entries[i].append({
                        "rule": work_item.rule.get("name", work_item.rule["match"]),
                        "action": "redact",
                        "path": work_item.element.get("path", "?"),
                    })
            all_fields.extend(fields)

    if not all_fields:
        return

    # Pre-compute NLP params once per unique NlpWork instance.
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
        _batch_detect_prewarm(adapter, list(texts), list(entities_tuple), threshold, language)

    # Phase C: Per-resource replacement with token_state isolation
    # Group fields by resource index for per-resource token_state
    fields_by_resource: dict[int, list[_TextField]] = {}
    for f in all_fields:
        fields_by_resource.setdefault(f.resource_idx, []).append(f)

    # Track which work items had changes (for manifest)
    work_item_changed: dict[int, dict[int, bool]] = {}  # resource_idx -> {work_item_id -> changed}

    for res_idx, fields in fields_by_resource.items():
        token_state: dict = {"next": {}, "map": {}, "reverse": {}}
        resource_changed: dict[int, bool] = {}

        for field in fields:
            wi_id = id(field.work_item)
            try:
                if field.is_xhtml:
                    result, changed = _apply_replacement_xhtml(field, adapter, token_state)
                else:
                    result, changed = _apply_replacement(field, adapter, token_state)

                if changed:
                    field.owner[field.key] = result
                    resource_changed[wi_id] = True
                elif wi_id not in resource_changed:
                    resource_changed[wi_id] = False
            except Exception:
                _log.error("nlp_batch_replace_failed res=%d key=%s — redacting", res_idx, field.key)
                field.owner[field.key] = "[REDACTED]"
                resource_changed[wi_id] = True

        work_item_changed[res_idx] = resource_changed

    # Phase D: Record manifest entries for NLP work items
    if _MANIFEST_ENABLED:
        for i, nlp_works in enumerate(all_nlp_works):
            if not nlp_works or resources[i] is None:
                continue
            res_changes = work_item_changed.get(i, {})
            for work_item in nlp_works:
                wi_id = id(work_item)
                changed = res_changes.get(wi_id, False)
                if work_item.action_type == "nlp_detect_act" and not changed:
                    # Text was scanned but no PII found — record as
                    # "scanned" so the scoring system knows this path
                    # was examined (prevents false-positive coverage gaps).
                    all_manifest_entries[i].append({
                        "rule": work_item.rule.get("name", work_item.rule["match"]),
                        "action": "nlp_detect_act/clean",
                        "path": work_item.element.get("path", "?"),
                    })
                    continue
                action_name = work_item.action_type
                all_manifest_entries[i].append({
                    "rule": work_item.rule.get("name", work_item.rule["match"]),
                    "action": action_name,
                    "path": work_item.element.get("path", "?"),
                })


def run_nlp_batch_single(
    resource: dict,
    nlp_work: list[NlpWork],
    manifest_entries: list[dict],
    processing_mode: str,
) -> None:
    """Run NLP batch for a single resource (N=1 fast path)."""
    run_nlp_batch_for_batch(
        [resource], [nlp_work], [manifest_entries], processing_mode
    )
