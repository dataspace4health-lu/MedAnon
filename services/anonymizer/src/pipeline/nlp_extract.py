"""Field & attachment text extraction for the NLP batch stage.

Split out of :mod:`pipeline.nlp_orchestrator`  this is the *extraction* half
(navigate to matched elements, decode Base64 attachments, discover inline
data: URIs) that feeds the detection/replacement orchestration. Keeping it
separate keeps the orchestrator focused on the batch detect→replace flow.

The orchestrator re-imports ``_FieldText``, ``_extract_fields``,
``_discover_text_attachments``, and the MIME constants, so existing module-level
references and test patch targets continue to resolve.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from utils.fhirpath import find_nodes
from pipeline.action_dispatcher import PHIDetectionTask

_log = logging.getLogger("medanon.nlp_batch")

# MIME types whose Base64-encoded payloads can be decoded to text and NLP-scrubbed.
# Everything else (image/*, application/pdf, …) is redacted entirely when
# the base64_encoded param is set  we cannot scrub opaque binary payloads.
_TEXT_MIME_TYPES = frozenset(
    {
        "text/plain",
        "text/html",
        "text/xml",
        "text/csv",
        "text/rtf",
        "application/json",
        "application/fhir+json",
        "application/fhir+xml",
        "application/xml",
    }
)

# FHIR field names that carry a MIME type describing a sibling ``data`` Base64Binary.
# FHIR Attachment uses ``contentType``; FHIR Signature uses ``sigFormat``.
# ``mimeType`` appears in some HL7v2-mapped extensions.
_MIME_INDICATOR_FIELDS: tuple[str, ...] = ("contentType", "sigFormat", "mimeType")

# Sentinel PHIDetectionTask shared by all heuristically discovered attachment fields.
# Using a single object means Phase B dedup groups them together and
# _resolve_nlp_params() is called once regardless of how many attachments
# are found across the batch.
_HEURISTIC_SENTINEL = PHIDetectionTask(
    rule={"name": "auto:attachment_scan", "match": "**"},
    element={"path": "**"},
    params={"entities": "healthcare", "threshold": 0.4, "language": "en"},
    action_type="nlp_detect_act",
)


# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


@dataclass
class _FieldText:
    """One text field extracted for NLP processing."""

    text: str
    owner: Any  # containing dict or list (for write-back)
    key: str | int  # dict key or list index
    is_xhtml: bool
    work_item: PHIDetectionTask  # back-reference for params/action_type
    resource_idx: int  # index into the parsed resources list
    nlp_params: tuple = ()  # (entities, threshold, language)  populated post-extraction
    base64_encoded: bool = (
        False  # True when text is Base64-decoded; write-back re-encodes
    )
    data_uri_prefix: str = (
        ""  # non-empty for data: URI fields; prepended to b64 result on write-back
    )
    path_hint: str = (
        ""  # JSON path of origin; set by heuristic scanner for manifest entries
    )


# ---------------------------------------------------------------------------
# Text extraction (replicates navigation from deidentify.py:145-207)
# ---------------------------------------------------------------------------


def _extract_fields(
    resource: dict, work_item: PHIDetectionTask, resource_idx: int
) -> list[_FieldText]:
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
        _log.error("nlp_batch_find_nodes_failed path=%s  will redact", path)
        return []

    fields: list[_FieldText] = []

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
                fields.append(
                    _FieldText(
                        text=current["div"],
                        owner=current,
                        key="div",
                        is_xhtml=True,
                        work_item=work_item,
                        resource_idx=resource_idx,
                    )
                )
            elif isinstance(current, str):
                fields.append(
                    _FieldText(
                        text=current,
                        owner=node,
                        key=field_key,
                        is_xhtml=True,
                        work_item=work_item,
                        resource_idx=resource_idx,
                    )
                )
        elif use_base64 and isinstance(current, str):
            # Attachment data field: decode Base64, check MIME type, enqueue for NLP.
            # Non-text MIME types (image/*, application/pdf, …) and undecodable blobs
            # are redacted in-place  we cannot scrub opaque binary payloads.
            mime = node.get("contentType", "").split(";")[0].strip().lower()
            if mime not in _TEXT_MIME_TYPES:
                _log.debug(
                    "base64_scrub: non-text mime=%r at path=%s  redacting data field",
                    mime,
                    path,
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
                # printable text  if >5% of bytes are control chars, it's binary.
                ctrl_ratio = sum(
                    1 for c in candidate if ord(c) < 32 and c not in "\t\n\r"
                ) / max(len(candidate), 1)
                if ctrl_ratio < 0.05:
                    decoded = candidate
                    is_b64 = True
            except (ValueError, UnicodeDecodeError, Exception):
                pass
            if decoded is None:
                # Fallback: data field already contains plain text (common with
                # inbound bundles that ignore the FHIR base64-only spec).  Scrub
                # in-place WITHOUT re-encoding so the field stays human-readable.
                # Require whitespace as proof of prose  strings like
                # "not!!valid==base64" (no spaces, contains non-base64 chars)
                # are corrupt payloads, not plain text, and must be redacted.
                has_whitespace = any(c.isspace() for c in current)
                if has_whitespace:
                    decoded = current
                    is_b64 = False
                    _log.info(
                        "base64_scrub: data field at path=%s is plain text  scrubbing without re-encoding",
                        path,
                    )
                else:
                    _log.error(
                        "base64_decode_failed path=%s  redacting data field", path
                    )
                    node[field_key] = ""
                    return
            is_xhtml = mime in ("text/html", "application/xml", "application/fhir+xml")
            fields.append(
                _FieldText(
                    text=decoded,
                    owner=node,
                    key=field_key,
                    is_xhtml=is_xhtml,
                    work_item=work_item,
                    resource_idx=resource_idx,
                    base64_encoded=is_b64,
                )
            )
        elif isinstance(current, str):
            fields.append(
                _FieldText(
                    text=current,
                    owner=node,
                    key=field_key,
                    is_xhtml=False,
                    work_item=work_item,
                    resource_idx=resource_idx,
                )
            )
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    fields.append(
                        _FieldText(
                            text=v,
                            owner=current,
                            key=i,
                            is_xhtml=False,
                            work_item=work_item,
                            resource_idx=resource_idx,
                        )
                    )

    _collect(nodes, key)
    return fields


def _discover_text_attachments(
    obj: Any,
    resource_idx: int,
    claimed: set[tuple],
    results: list[_FieldText],
    path: str = "",
) -> None:
    """Recursively scan a resource for Base64-encoded text and enqueue for NLP.

    Three structural patterns, independent of resource type or nesting depth:

    Pattern 1  MIME indicator + ``data`` field:
        Any dict with a MIME-type field (``contentType``, ``sigFormat``, ``mimeType``)
        alongside a ``data`` Base64Binary. Covers Attachment, Binary, Signature, and
        any extension following the same convention.
        text/* → decode + enqueue; binary → redact in-place.

    Pattern 2  ``data:`` URI in ``url`` field:
        Attachment.url may carry inline content as ``data:[mime];base64,<b64>``.
        Scrubbed text is re-encoded and the data: URI reconstructed on write-back.

    Pattern 3  standalone ``*Base64Binary`` fields:
        Polymorphic value[x] fields (``valueBase64Binary``, etc.) carry no MIME type.
        Strict UTF-8 decode attempted; success means likely human-readable text.
        Binary payloads fail strict UTF-8 and are silently skipped.
    """
    if not isinstance(obj, dict):
        if isinstance(obj, list):
            for i, item in enumerate(obj):
                _discover_text_attachments(
                    item, resource_idx, claimed, results, f"{path}[{i}]"
                )
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
                        decoded = base64.b64decode(data_val).decode(
                            "utf-8", errors="replace"
                        )
                    except Exception:
                        _log.error(
                            "heuristic_base64_decode_failed path=%s  redacting",
                            field_path,
                        )
                        obj["data"] = ""
                    else:
                        is_xhtml = mime in (
                            "text/html",
                            "application/xml",
                            "application/fhir+xml",
                        )
                        results.append(
                            _FieldText(
                                text=decoded,
                                owner=obj,
                                key="data",
                                is_xhtml=is_xhtml,
                                work_item=_HEURISTIC_SENTINEL,
                                resource_idx=resource_idx,
                                base64_encoded=True,
                                path_hint=field_path,
                            )
                        )
                        claimed.add(claim_key)
                else:
                    _log.debug(
                        "heuristic_attachment: non-text mime=%r at %s  redacting data",
                        mime,
                        field_path,
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
                b64_content = url_val[comma_idx + 1 :]
                if ";base64" in header:
                    uri_mime = header.split(";")[0].strip().lower()
                    if uri_mime in _TEXT_MIME_TYPES:
                        decoded = base64.b64decode(b64_content).decode(
                            "utf-8", errors="replace"
                        )
                        is_xhtml = uri_mime in (
                            "text/html",
                            "application/xml",
                            "application/fhir+xml",
                        )
                        field_path = f"{path}.url" if path else "url"
                        results.append(
                            _FieldText(
                                text=decoded,
                                owner=obj,
                                key="url",
                                is_xhtml=is_xhtml,
                                work_item=_HEURISTIC_SENTINEL,
                                resource_idx=resource_idx,
                                base64_encoded=True,
                                data_uri_prefix=f"data:{uri_mime};base64,",
                                path_hint=field_path,
                            )
                        )
                        claimed.add(claim_key)
            except (ValueError, Exception):
                pass  # malformed data: URI  leave untouched

    # --- Pattern 3: standalone *Base64Binary fields + recurse ---
    for k, v in obj.items():
        child_path = f"{path}.{k}" if path else k
        if k.endswith("Base64Binary") and isinstance(v, str) and v:
            claim_key = (id(obj), k)
            if claim_key not in claimed:
                try:
                    decoded = base64.b64decode(v).decode("utf-8", errors="strict")
                except (ValueError, UnicodeDecodeError):
                    pass  # binary payload  skip silently
                else:
                    results.append(
                        _FieldText(
                            text=decoded,
                            owner=obj,
                            key=k,
                            is_xhtml=False,
                            work_item=_HEURISTIC_SENTINEL,
                            resource_idx=resource_idx,
                            base64_encoded=True,
                            path_hint=child_path,
                        )
                    )
                    claimed.add(claim_key)
        _discover_text_attachments(v, resource_idx, claimed, results, child_path)
