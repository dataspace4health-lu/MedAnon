"""Pass 1.5: batch NLP detection and replacement.

Collects texts from all deferred NlpWork items, runs entity detection
in a single batch (one HTTP call for remote, or cache-prewarming for local),
then applies replacements per-resource with proper token_state isolation.

Mirrors the gPAS batch pattern in ``gpas_orchestrator.py``.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from utils.fhirpath import find_nodes
from pipeline.manifest import _MANIFEST_ENABLED
from pipeline.action_dispatcher import NlpWork

_log = logging.getLogger("medanon.nlp_batch")

# MIME types whose Base64-encoded payloads can be decoded to text and NLP-scrubbed.
# Everything else (image/*, application/pdf, …) is redacted entirely when
# the base64_encoded param is set — we cannot scrub opaque binary payloads.
_TEXT_MIME_TYPES = frozenset({
    "text/plain", "text/html", "text/xml", "text/csv", "text/rtf",
    "application/json", "application/fhir+json", "application/fhir+xml",
    "application/xml",
})

# FHIR field names that carry a MIME type describing a sibling ``data`` Base64Binary.
# FHIR Attachment uses ``contentType``; FHIR Signature uses ``sigFormat``.
# ``mimeType`` appears in some HL7v2-mapped extensions.
_MIME_INDICATOR_FIELDS: tuple[str, ...] = ("contentType", "sigFormat", "mimeType")

# Sentinel NlpWork shared by all heuristically discovered attachment fields.
# Using a single object means Phase B dedup groups them together and
# _resolve_nlp_params() is called once regardless of how many attachments
# are found across the batch.
_HEURISTIC_SENTINEL = NlpWork(
    rule={"name": "auto:attachment_scan", "match": "**"},
    element={"path": "**"},
    params={"entities": "healthcare", "threshold": 0.4, "language": "en"},
    action_type="nlp_detect_act",
)


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
    base64_encoded: bool = False  # True when text is Base64-decoded; write-back re-encodes
    data_uri_prefix: str = ""  # non-empty for data: URI fields; prepended to b64 result on write-back
    path_hint: str = ""  # JSON path of origin; set by heuristic scanner for manifest entries


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
    use_base64 = bool(work_item.params.get("base64_encoded", False))

    try:
        nodes = find_nodes(resource, parent_path, [])
    except Exception:
        _log.error("nlp_batch_find_nodes_failed path=%s — will redact", path)
        return []

    fields: list[_TextField] = []

    def _collect(node, field_key):
        if isinstance(node, list):
            for item in node:
                _collect(item, field_key)
            return
        if not isinstance(node, dict) or field_key not in node:
            return
        current = node[field_key]
        if use_html:
            if isinstance(current, dict) and isinstance(current.get("div"), str):
                fields.append(_TextField(
                    text=current["div"], owner=current, key="div",
                    is_xhtml=True, work_item=work_item, resource_idx=resource_idx,
                ))
            elif isinstance(current, str):
                fields.append(_TextField(
                    text=current, owner=node, key=field_key,
                    is_xhtml=True, work_item=work_item, resource_idx=resource_idx,
                ))
        elif use_base64 and isinstance(current, str):
            # Attachment data field: decode Base64, check MIME type, enqueue for NLP.
            # Non-text MIME types (image/*, application/pdf, …) and undecodable blobs
            # are redacted in-place — we cannot scrub opaque binary payloads.
            mime = node.get("contentType", "").split(";")[0].strip().lower()
            if mime not in _TEXT_MIME_TYPES:
                _log.debug(
                    "base64_scrub: non-text mime=%r at path=%s — redacting data field",
                    mime, path,
                )
                node[field_key] = ""
                return
            decoded: str | None = None
            is_b64 = False
            try:
                # Strict decode: rejects plain text that happens to start with valid
                # base64 chars but contains non-base64 separators (spaces, punct).
                raw = base64.b64decode(current, validate=True)
                candidate = raw.decode("utf-8", errors="strict")
                # Heuristic: real base64 payload is always >=4 chars and decodes to
                # printable text — if >5% of bytes are control chars, it's binary.
                ctrl_ratio = sum(1 for c in candidate if ord(c) < 32 and c not in "\t\n\r") / max(len(candidate), 1)
                if ctrl_ratio < 0.05:
                    decoded = candidate
                    is_b64 = True
            except (ValueError, UnicodeDecodeError, Exception):
                pass
            if decoded is None:
                # Fallback: data field already contains plain text (common with
                # inbound bundles that ignore the FHIR base64-only spec).  Scrub
                # in-place WITHOUT re-encoding so the field stays human-readable.
                # Require whitespace as proof of prose — strings like
                # "not!!valid==base64" (no spaces, contains non-base64 chars)
                # are corrupt payloads, not plain text, and must be redacted.
                has_whitespace = any(c.isspace() for c in current)
                if has_whitespace:
                    decoded = current
                    is_b64 = False
                    _log.info(
                        "base64_scrub: data field at path=%s is plain text — scrubbing without re-encoding",
                        path,
                    )
                else:
                    _log.error(
                        "base64_decode_failed path=%s — redacting data field", path
                    )
                    node[field_key] = ""
                    return
            is_xhtml = mime in ("text/html", "application/xml", "application/fhir+xml")
            fields.append(_TextField(
                text=decoded, owner=node, key=field_key,
                is_xhtml=is_xhtml, work_item=work_item, resource_idx=resource_idx,
                base64_encoded=is_b64,
            ))
        elif isinstance(current, str):
            fields.append(_TextField(
                text=current, owner=node, key=field_key,
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


def _discover_text_attachments(
    obj: Any,
    resource_idx: int,
    claimed: set[tuple],
    results: list[_TextField],
    path: str = "",
) -> None:
    """Recursively scan a resource for Base64-encoded text and enqueue for NLP.

    Three structural patterns, independent of resource type or nesting depth:

    Pattern 1 — MIME indicator + ``data`` field:
        Any dict with a MIME-type field (``contentType``, ``sigFormat``, ``mimeType``)
        alongside a ``data`` Base64Binary. Covers Attachment, Binary, Signature, and
        any extension following the same convention.
        text/* → decode + enqueue; binary → redact in-place.

    Pattern 2 — ``data:`` URI in ``url`` field:
        Attachment.url may carry inline content as ``data:[mime];base64,<b64>``.
        Scrubbed text is re-encoded and the data: URI reconstructed on write-back.

    Pattern 3 — standalone ``*Base64Binary`` fields:
        Polymorphic value[x] fields (``valueBase64Binary``, etc.) carry no MIME type.
        Strict UTF-8 decode attempted; success means likely human-readable text.
        Binary payloads fail strict UTF-8 and are silently skipped.
    """
    if not isinstance(obj, dict):
        if isinstance(obj, list):
            for i, item in enumerate(obj):
                _discover_text_attachments(item, resource_idx, claimed, results, f"{path}[{i}]")
        return

    # --- Pattern 1: MIME indicator + ``data`` ---
    data_val = obj.get("data")
    if isinstance(data_val, str) and data_val:
        mime: str | None = None
        for mime_field in _MIME_INDICATOR_FIELDS:
            raw = obj.get(mime_field)
            if isinstance(raw, str) and raw:
                mime = raw.split(";")[0].strip().lower()
                break
        if mime is not None:
            field_path = f"{path}.data" if path else "data"
            claim_key = (id(obj), "data")
            if claim_key not in claimed:
                if mime in _TEXT_MIME_TYPES:
                    try:
                        decoded = base64.b64decode(data_val).decode("utf-8", errors="replace")
                    except Exception:
                        _log.error("heuristic_base64_decode_failed path=%s — redacting", field_path)
                        obj["data"] = ""
                    else:
                        is_xhtml = mime in ("text/html", "application/xml", "application/fhir+xml")
                        results.append(_TextField(
                            text=decoded, owner=obj, key="data", is_xhtml=is_xhtml,
                            work_item=_HEURISTIC_SENTINEL, resource_idx=resource_idx,
                            base64_encoded=True, path_hint=field_path,
                        ))
                        claimed.add(claim_key)
                else:
                    _log.debug(
                        "heuristic_attachment: non-text mime=%r at %s — redacting data",
                        mime, field_path,
                    )
                    obj["data"] = ""

    # --- Pattern 2: data: URI in ``url`` field ---
    url_val = obj.get("url")
    if isinstance(url_val, str) and url_val.startswith("data:"):
        claim_key = (id(obj), "url")
        if claim_key not in claimed:
            try:
                comma_idx = url_val.index(",")
                header = url_val[5:comma_idx]
                b64_content = url_val[comma_idx + 1:]
                if ";base64" in header:
                    uri_mime = header.split(";")[0].strip().lower()
                    if uri_mime in _TEXT_MIME_TYPES:
                        decoded = base64.b64decode(b64_content).decode("utf-8", errors="replace")
                        is_xhtml = uri_mime in ("text/html", "application/xml", "application/fhir+xml")
                        field_path = f"{path}.url" if path else "url"
                        results.append(_TextField(
                            text=decoded, owner=obj, key="url", is_xhtml=is_xhtml,
                            work_item=_HEURISTIC_SENTINEL, resource_idx=resource_idx,
                            base64_encoded=True,
                            data_uri_prefix=f"data:{uri_mime};base64,",
                            path_hint=field_path,
                        ))
                        claimed.add(claim_key)
            except (ValueError, Exception):
                pass  # malformed data: URI — leave untouched

    # --- Pattern 3: standalone *Base64Binary fields + recurse ---
    for k, v in obj.items():
        child_path = f"{path}.{k}" if path else k
        if k.endswith("Base64Binary") and isinstance(v, str) and v:
            claim_key = (id(obj), k)
            if claim_key not in claimed:
                try:
                    decoded = base64.b64decode(v).decode("utf-8", errors="strict")
                except (ValueError, UnicodeDecodeError):
                    pass  # binary payload — skip silently
                else:
                    results.append(_TextField(
                        text=decoded, owner=obj, key=k, is_xhtml=False,
                        work_item=_HEURISTIC_SENTINEL, resource_idx=resource_idx,
                        base64_encoded=True, path_hint=child_path,
                    ))
                    claimed.add(claim_key)
        _discover_text_attachments(v, resource_idx, claimed, results, child_path)


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
        from pipeline.deidentify import (
            _DEFAULT_ENTITY_ACTIONS,
            _merge_overlapping_spans,
            _replace_span,
        )

        entity_actions = {**_DEFAULT_ENTITY_ACTIONS, **(field.work_item.params.get("entity_actions") or {})}
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

    # Phase A: Extract all text fields from config-rule NlpWork items.
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

    # Phase A1: Heuristic attachment scan — find any {contentType, data} pair
    # anywhere in each resource, regardless of resource type or nesting depth.
    # Runs after Phase A so that config-rule fields are already in the claimed set;
    # this prevents double-processing nodes that are covered by explicit rules.
    claimed: set[tuple] = {(id(f.owner), f.key) for f in all_fields}
    heuristic_fields: list[_TextField] = []
    for i, resource in enumerate(resources):
        if resource is None:
            continue
        _discover_text_attachments(resource, i, claimed, heuristic_fields)
    if heuristic_fields:
        _log.debug("heuristic_attachment_scan: found %d text attachment(s)", len(heuristic_fields))
    all_fields.extend(heuristic_fields)

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

    # Track which work items had changes (for manifest).
    # Heuristic fields all share _HEURISTIC_SENTINEL so they are tracked
    # separately by (resource_idx, path_hint) to emit per-field manifest entries.
    work_item_changed: dict[int, dict[int, bool]] = {}  # resource_idx -> {work_item_id -> changed}
    heuristic_changed: dict[int, list[str]] = {}        # resource_idx -> [path_hints that changed]

    def _run_resource(
        res_idx: int, fields: list[_TextField]
    ) -> tuple[int, dict[int, bool], list[str]]:
        """Apply NLP replacements for one resource.

        Thread-safe: each resource owns its token_state and writes only to
        its own resource dict entries — no shared mutable state across calls.
        """
        token_state: dict = {"next": {}, "map": {}, "reverse": {}}
        resource_changed: dict[int, bool] = {}
        heuristic_paths: list[str] = []

        for field in fields:
            wi_id = id(field.work_item)
            try:
                if field.is_xhtml:
                    result, changed = _apply_replacement_xhtml(field, adapter, token_state)
                else:
                    result, changed = _apply_replacement(field, adapter, token_state)

                if changed:
                    if field.base64_encoded:
                        result = base64.b64encode(result.encode("utf-8")).decode("ascii")
                    if field.data_uri_prefix:
                        result = field.data_uri_prefix + result
                    field.owner[field.key] = result
                    if field.work_item is _HEURISTIC_SENTINEL:
                        heuristic_paths.append(field.path_hint)
                    else:
                        resource_changed[wi_id] = True
                elif wi_id not in resource_changed and field.work_item is not _HEURISTIC_SENTINEL:
                    resource_changed[wi_id] = False
            except Exception:
                _log.error("nlp_batch_replace_failed res=%d key=%s — redacting", res_idx, field.key)
                field.owner[field.key] = "[REDACTED]"
                if field.work_item is _HEURISTIC_SENTINEL:
                    heuristic_paths.append(field.path_hint)
                else:
                    resource_changed[wi_id] = True

        return res_idx, resource_changed, heuristic_paths

    if len(fields_by_resource) > 1:
        # Parallel path: each resource is independent (separate token_state +
        # writes to its own dict entries).  Mirrors the parallel finalization
        # pattern in pipeline/processor.py.
        from utils.thread_pool import get_executor
        from concurrent.futures import as_completed

        pool = get_executor()
        futures: dict = {}
        sequential_fallback: list[tuple[int, list[_TextField]]] = []

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
                idx, rc, hp = fut.result()
                work_item_changed[idx] = rc
                if hp:
                    heuristic_changed[idx] = hp
            except Exception:
                _log.error("nlp_phase_c_error res=%d", futures[fut])

        for res_idx, fields in sequential_fallback:
            idx, rc, hp = _run_resource(res_idx, fields)
            work_item_changed[idx] = rc
            if hp:
                heuristic_changed[idx] = hp
    else:
        # Single resource — skip thread pool overhead
        for res_idx, fields in fields_by_resource.items():
            idx, rc, hp = _run_resource(res_idx, fields)
            work_item_changed[idx] = rc
            if hp:
                heuristic_changed[idx] = hp

    # Phase D: Record manifest entries for config-rule NLP work items
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

        # Phase D1: Manifest entries for heuristically discovered attachment fields
        for res_idx, paths in heuristic_changed.items():
            for path in paths:
                all_manifest_entries[res_idx].append({
                    "rule": "auto:attachment_scan",
                    "action": "nlp_detect_act",
                    "path": path,
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
