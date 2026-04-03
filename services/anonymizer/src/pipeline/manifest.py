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


def _build_manifest_tag(manifest_entries: list[dict]) -> dict:
    """Build a FHIR meta.tag entry summarising applied transformations."""
    return {
        "system": MANIFEST_SYSTEM,
        "code": "transformation-manifest",
        "display": _json_dumps(manifest_entries),
    }


def _attach_manifest(resource: dict, manifest_entries: list[dict]) -> None:
    """Attach the transformation manifest tag to a resource's meta.tag."""
    if not manifest_entries or not isinstance(resource, dict):
        return
    meta = resource.setdefault("meta", {})
    tags = meta.setdefault("tag", [])
    tags.append(_build_manifest_tag(manifest_entries))
