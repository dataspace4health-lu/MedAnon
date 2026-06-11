"""Transformation manifest helpers.

When MEDANON_MANIFEST_ENABLED is set, each processed resource's meta.tag
receives a compact JSON summary of which rules fired (rule name/match, action,
FHIRPath — no PHI values).
"""

from __future__ import annotations

import os
from utils.json_fast import dumps as _json_dumps

_MANIFEST_ENABLED: bool = os.environ.get(
    "MEDANON_MANIFEST_ENABLED", "false"
).strip().lower() in ("1", "true", "yes")

MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest"
MANIFEST_FULL_EXT_URL = "https://medanon.local/transformation-manifest-full"

# HAPI FHIR's hfj_tag_def.tag_display column is varchar(200); anything longer
# is silently truncated, losing audit trail.  When the JSON-serialised entries
# would exceed this we collapse to a count summary in ``display`` (so HAPI is
# happy) AND attach the full payload as a tag-level ``extension`` (FHIR
# permits extensions on Coding, and HAPI stores them in hfj_res_ver — no 200
# char cap).  The scorer prefers the extension over the truncated display.
_MANIFEST_DISPLAY_MAX = 200


def _build_manifest_tag(manifest_entries: list[dict]) -> dict:
    """Build a FHIR meta.tag entry summarising applied transformations."""
    full = _json_dumps(manifest_entries)
    if len(full) <= _MANIFEST_DISPLAY_MAX:
        return {
            "system": MANIFEST_SYSTEM,
            "code": "transformation-manifest",
            "display": full,
        }
    # Compact summary that fits in 200 chars: count + first N action names.
    actions = [e.get("action", "?") for e in manifest_entries]
    summary = f"rules={len(manifest_entries)} actions={','.join(actions[:5])}"
    if len(actions) > 5:
        summary += f",+{len(actions) - 5}more"
    return {
        "system": MANIFEST_SYSTEM,
        "code": "transformation-manifest",
        "display": summary[:_MANIFEST_DISPLAY_MAX],
        # Full payload as an extension on the Coding — uncapped, FHIR-valid,
        # and consumed by the scorer's ``_extract_manifest_entries``.
        "extension": [
            {
                "url": MANIFEST_FULL_EXT_URL,
                "valueString": full,
            }
        ],
    }


def _attach_manifest(resource: dict, manifest_entries: list[dict]) -> None:
    """Attach the transformation manifest tag to a resource's meta.tag."""
    if not manifest_entries or not isinstance(resource, dict):
        return
    meta = resource.setdefault("meta", {})
    tags = meta.setdefault("tag", [])
    tags.append(_build_manifest_tag(manifest_entries))


_SUPPRESSED_SYSTEM = "https://medanon.local/privacy-suppressed"


def attach_suppressed_tag(resource: dict, reason: str = "k-anonymity") -> None:
    """Attach a suppression marker to *resource*'s meta.tag.

    Called when a Patient (or linked resource) is omitted from the output
    due to the risk-driven generalization plan.  In normal operation the
    resource is simply dropped; this helper is provided for audit logging
    or when ``emit_suppressed_stub=true`` is needed in future.
    """
    meta = resource.setdefault("meta", {})
    tags = meta.setdefault("tag", [])
    tags.append(
        {
            "system": _SUPPRESSED_SYSTEM,
            "code": "patient-suppressed",
            "display": f"Suppressed for {reason} guarantee",
        }
    )


def extract_manifest_entries(resource: dict) -> list[dict]:
    """Extract transformation manifest entries from a resource's meta.tag.

    Prefers the full payload stored in the tag's ``extension[].valueString``
    (used when the JSON exceeds HAPI's 200-char ``display`` cap) and falls
    back to parsing ``display`` for short manifests.

    This is the single source of truth — all scoring callers must use it,
    otherwise large manifests appear empty and PHI-bearing resources falsely
    fail the privacy gate (composite=0, grade F).
    """
    if not isinstance(resource, dict):
        return []
    meta = resource.get("meta")
    if not isinstance(meta, dict):
        return []
    from utils.json_fast import loads as _json_loads

    for tag in meta.get("tag", []) or []:
        if not (isinstance(tag, dict) and tag.get("system") == MANIFEST_SYSTEM):
            continue
        # 1) Prefer full payload in the extension (uncapped).
        for ext in tag.get("extension", []) or []:
            if isinstance(ext, dict) and ext.get("url") == MANIFEST_FULL_EXT_URL:
                payload = ext.get("valueString", "")
                if payload:
                    try:
                        entries = _json_loads(payload)
                        if isinstance(entries, list):
                            return entries
                    except (ValueError, TypeError):
                        pass
        # 2) Fall back to display (only valid JSON when JSON < 200 chars).
        display = tag.get("display", "")
        if display:
            try:
                entries = _json_loads(display)
                if isinstance(entries, list):
                    return entries
            except (ValueError, TypeError):
                pass
    return []
