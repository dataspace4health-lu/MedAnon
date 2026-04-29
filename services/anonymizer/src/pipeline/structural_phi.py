"""Config-independent structural PHI heuristics.

These patterns are structurally recognisable regardless of which config
profile is active and cannot be expressed as simple single-level FHIRPath
rules without recursive-descent support:

1. Geolocation — ``{url: "latitude"|"longitude", valueDecimal: X}`` anywhere
   in the resource tree (Patient.address, Location, Organization, custom
   extensions at any nesting depth).  Value is zeroed in place.

2. Extension text labels — ``{url: "text", valueString: X}`` inside any
   ``extension`` array (US Core race/ethnicity text, and any other complex
   extension following the same FHIR sub-extension pattern).  Value is
   replaced with ``"[REDACTED]"``.
"""

from __future__ import annotations

_GEO_COORD_URLS: frozenset[str] = frozenset({"latitude", "longitude"})


def _scan(obj: object, *, _in_ext: bool) -> None:
    if isinstance(obj, dict):
        url = obj.get("url")
        if url in _GEO_COORD_URLS and isinstance(obj.get("valueDecimal"), (int, float)):
            obj["valueDecimal"] = 0.0
        if _in_ext and url == "text" and isinstance(obj.get("valueString"), str) and obj["valueString"]:
            obj["valueString"] = "[REDACTED]"
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                _scan(v, _in_ext=_in_ext or k == "extension")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _scan(item, _in_ext=_in_ext)


def apply_structural_heuristics(resource: dict) -> None:
    """Mask geolocation coordinates and extension text labels in *resource* in-place."""
    _scan(resource, _in_ext=False)
