"""Config-independent structural PHI heuristics.

These patterns are structurally recognisable regardless of which config
profile is active and cannot be expressed as simple single-level FHIRPath
rules without recursive-descent support:

1. Geolocation  ``{url: "latitude"|"longitude", valueDecimal: X}`` anywhere
   in the resource tree (Patient.address, Location, Organization, custom
   extensions at any nesting depth).  Value is zeroed in place.

2. Extension text labels  ``{url: "text", valueString: X}`` inside any
   ``extension`` array (US Core race/ethnicity text, and any other complex
   extension following the same FHIR sub-extension pattern).  Value is
   replaced with ``"[REDACTED]"``.

This pass is **opt-in** (``MEDANON_STRUCTURAL_PHI_ENABLED``, default off)  the
config rules are normally the single source of truth.  When enabled, every
change is reported back to the caller (a list of ``{path, action}`` records) so
it appears in the transformation manifest rather than mutating silently.
"""

from __future__ import annotations

_GEO_COORD_URLS: frozenset[str] = frozenset({"latitude", "longitude"})


def _scan(obj: object, *, _in_ext: bool, _path: str, changes: list[dict]) -> None:
    if isinstance(obj, dict):
        url = obj.get("url")
        if url in _GEO_COORD_URLS and isinstance(obj.get("valueDecimal"), (int, float)):
            if obj["valueDecimal"] != 0.0:
                obj["valueDecimal"] = 0.0
                changes.append(
                    {"path": f"{_path}.valueDecimal", "action": "structural_geo"}
                )
        if (
            _in_ext
            and url == "text"
            and isinstance(obj.get("valueString"), str)
            and obj["valueString"]
        ):
            obj["valueString"] = "[REDACTED]"
            changes.append(
                {"path": f"{_path}.valueString", "action": "structural_ext_text"}
            )
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _scan(
                    v,
                    _in_ext=_in_ext or k == "extension",
                    _path=f"{_path}.{k}" if _path else k,
                    changes=changes,
                )
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, (dict, list)):
                _scan(item, _in_ext=_in_ext, _path=f"{_path}[{i}]", changes=changes)


def apply_structural_heuristics(resource: dict) -> list[dict]:
    """Mask geolocation coordinates and extension text labels in *resource* in-place.

    Returns a list of ``{"path": <FHIRPath>, "action": <label>}`` records for
    every value it changed, so the caller can record them in the manifest. An
    empty list means nothing matched.
    """
    changes: list[dict] = []
    root = resource.get("resourceType", "") if isinstance(resource, dict) else ""
    _scan(resource, _in_ext=False, _path=root, changes=changes)
    return changes
