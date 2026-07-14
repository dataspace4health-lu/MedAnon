"""Minimisation Report builder (D7.2 §3).

Reuses the always-on fast PII scanner (:func:`integrations.ai.agents.pii_detector.
detect_pii_fast`) to locate identifiers, then:

- classifies each finding as a **direct identifier** (critical: names/SSN/MRN) or
  a **quasi-identifier** (high/medium: address, date, phone, email, IP);
- recommends a lower-granularity representation per D7.2 §3.3 (year instead of
  exact date, region/ZIP-prefix instead of full postcode, redact/pseudonymise
  for direct identifiers);
- flags variables not covered by a declared purpose (``declared_paths``) as
  candidates for exclusion (purpose limitation, §3.4).

Output is a pure JSON-serialisable dict; no transformation is performed.
"""

from __future__ import annotations

from typing import Any

# Map a detected PII *type* to (classification, generalisation kind, recommendation).
# ``kind`` links to pipeline.privacy.hierarchies where a climbable hierarchy exists.
_TYPE_RULES: dict[str, dict[str, str]] = {
    "name": {"class": "direct_identifier", "action": "redact or pseudonymise (gPAS)"},
    "ssn": {"class": "direct_identifier", "action": "redact or pseudonymise (gPAS)"},
    "mrn": {"class": "direct_identifier", "action": "pseudonymise (gPAS)"},
    "phone": {"class": "quasi_identifier", "action": "redact"},
    "email": {"class": "quasi_identifier", "action": "redact"},
    "address": {
        "class": "quasi_identifier",
        "kind": "zip",
        "action": "generalise to region / 3-digit ZIP prefix (WHERE dimension)",
    },
    "date": {
        "class": "quasi_identifier",
        "kind": "date",
        "action": "generalise to year or year-month (WHEN dimension)",
    },
    "ip": {"class": "quasi_identifier", "action": "redact or mask"},
    "other": {"class": "quasi_identifier", "action": "review necessity"},
}

_SEVERITY_CLASS = {
    "critical": "direct_identifier",
    "high": "quasi_identifier",
    "medium": "quasi_identifier",
}


def _classify(det: dict) -> dict[str, Any]:
    dtype = str(det.get("type", "other")).lower()
    rule = _TYPE_RULES.get(dtype)
    if rule is None:
        # Fall back to severity-based classification.
        cls = _SEVERITY_CLASS.get(det.get("severity", "medium"), "quasi_identifier")
        rule = {"class": cls, "action": "review necessity"}
    return {
        "path": det.get("field_path", ""),
        "type": dtype,
        "severity": det.get("severity", "medium"),
        "classification": rule["class"],
        "kind": rule.get("kind"),
        "recommendation": rule["action"],
    }


# Structural (semantic) quasi-identifiers D7.2 §3.3.1 names but regex/NER can't
# see  demographic FHIR fields. Each: (Patient sub-path, type, recommendation,
# Art 9(1) special-category). "Special" values reveal racial/ethnic/religious
# origin → §3.3.1 flags them for special safeguards.
_STRUCTURAL_QI_FIELDS: list[tuple[str, str, str, bool]] = [
    (
        "maritalStatus",
        "marital_status",
        "group/classify to the lowest useful detail (WHO dimension §3.3.1)",
        False,
    ),
    (
        "communication",
        "language",
        "retain only if strictly relevant; else drop (§3.3.1)",
        False,
    ),
    ("multipleBirthBoolean", "multiple_birth", "review necessity (§3.3.1)", False),
]

# Extensions matched by URL substring → (type, Art 9(1) special-category).
_QI_EXTENSION_URLS: dict[str, tuple[str, bool]] = {
    "us-core-race": ("race", True),
    "race": ("race", True),
    "us-core-ethnicity": ("ethnicity", True),
    "ethnicity": ("ethnicity", True),
    "patient-nationality": ("nationality", False),
    "nationality": ("nationality", False),
    "patient-religion": ("religion", True),
    "religion": ("religion", True),
}


def _scan_structural_qis(resources: list[dict]) -> list[dict[str, Any]]:
    """Detect semantic demographic quasi-identifiers (§3.3.1) on Patients.

    Regex/NER PII scanning cannot classify marital status, ethnicity, nationality,
    language or religion  they are structured coded fields. This scan reports
    one quasi-identifier finding per distinct field present, flagging
    special-category (Art 9(1)) ones.
    """
    found: dict[str, dict[str, Any]] = {}
    for r in resources:
        if not isinstance(r, dict) or r.get("resourceType") != "Patient":
            continue
        for field, dtype, rec, special in _STRUCTURAL_QI_FIELDS:
            if r.get(field) not in (None, "", [], {}):
                path = f"Patient.{field}"
                found.setdefault(
                    path,
                    {
                        "path": path,
                        "type": dtype,
                        "severity": "medium",
                        "classification": "quasi_identifier",
                        "kind": None,
                        "recommendation": rec,
                        "special_category": special,
                    },
                )
        for ext in r.get("extension") or []:
            if not isinstance(ext, dict):
                continue
            url = str(ext.get("url", "")).lower()
            for needle, (dtype, special) in _QI_EXTENSION_URLS.items():
                if needle in url:
                    path = f"Patient.extension[{dtype}]"
                    found.setdefault(
                        path,
                        {
                            "path": path,
                            "type": dtype,
                            "severity": "medium",
                            "classification": "quasi_identifier",
                            "kind": None,
                            "recommendation": (
                                "special-category data (Art 9(1))  retain only with "
                                "explicit justification + safeguards (§3.3.1)"
                                if special
                                else "group to lowest useful detail (§3.3.1)"
                            ),
                            "special_category": special,
                        },
                    )
                    break
    return list(found.values())


def assess_minimisation(
    resources: list[dict],
    *,
    declared_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Produce a Minimisation Report for *resources*.

    Args:
        resources: FHIR resource dicts (the dataset to assess).
        declared_paths: optional list of FHIRPaths the data user justified for
            the stated purpose. Findings whose path is not covered by any
            declared path are flagged as ``unjustified`` (purpose limitation).

    Returns a JSON-serialisable report: ``direct_identifiers``,
    ``quasi_identifiers``, ``recommendations``, ``unjustified``, ``summary``.
    """
    from integrations.ai.agents.pii_detector import detect_pii_fast

    detections = detect_pii_fast(resources) if resources else []
    classified = [_classify(d) for d in detections]

    # Add semantic demographic QIs (§3.3.1) that regex/NER cannot see.
    classified.extend(_scan_structural_qis(resources) if resources else [])

    direct = [c for c in classified if c["classification"] == "direct_identifier"]
    quasi = [c for c in classified if c["classification"] == "quasi_identifier"]
    special = [c for c in classified if c.get("special_category")]

    # Recommendations = every finding that has an actionable granularity change
    # or removal (i.e. all of them), de-duplicated by path+type.
    seen: set[tuple[str, str]] = set()
    recommendations: list[dict] = []
    for c in classified:
        key = (c["path"], c["type"])
        if key in seen:
            continue
        seen.add(key)
        recommendations.append(
            {
                "path": c["path"],
                "recommendation": c["recommendation"],
                "kind": c["kind"],
            }
        )

    # Purpose limitation: flag findings not covered by a declared path prefix.
    unjustified: list[dict] = []
    if declared_paths is not None:
        allowed = tuple(declared_paths)
        for c in classified:
            path = c["path"]
            if path and not any(path == a or path.startswith(a + ".") for a in allowed):
                unjustified.append({"path": path, "type": c["type"]})

    return {
        "direct_identifiers": direct,
        "quasi_identifiers": quasi,
        "special_category": special,
        "recommendations": recommendations,
        "unjustified": unjustified,
        "summary": {
            "total_findings": len(classified),
            "direct_identifier_count": len(direct),
            "quasi_identifier_count": len(quasi),
            "special_category_count": len(special),
            "unjustified_count": len(unjustified),
            "purpose_check": declared_paths is not None,
        },
    }
