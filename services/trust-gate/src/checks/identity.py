"""Patient identity SAMs (HL7 EHR WG 2026 / Verily cross-resource patterns).

PATIENT_IDENTITY_STABLE checks two things within the batch:
  1. Patient.link is absent or uses a known reconciliation type (not a conflict).
  2. No two Patient resources share the same identifier system+value — which
     would indicate a merge collision or a linking error.

This is a plausibility / situationally_implausible check (HDQT), but severity
is WARNING (CONDITIONAL_PASS) — merged patients still need de-identification,
so a BLOCK would be too disruptive. The check is only active when there are two
or more Patient resources in the batch.

Per Verily FHIR DQ patterns: single-resource streaming gates cannot do n-ary or
population checks; this check explicitly models the structural boundary: it
operates on the batch as a whole and is skipped for single-resource assessments.
"""

from __future__ import annotations

import logging

from constants import threshold_for
from passport import CheckResult, redact_token

_log = logging.getLogger("trust_gate.checks.identity")

# Patient.link.type values that indicate an intentional merge / redirect.
# Types NOT in this set (e.g. absent type, or unknown type) are treated as
# unqualified links → possible ambiguity.
_KNOWN_LINK_TYPES = frozenset({"replaced-by", "replaces", "refer", "seealso"})


def evaluate_patient_identity(
    resources: list[dict],
    thresholds: dict[str, float] | None = None,
) -> CheckResult:
    """PATIENT_IDENTITY_STABLE SAM.

    Severity: warning (CONDITIONAL_PASS on FAIL, not BLOCK).
    Skipped when fewer than 2 Patient resources are present (n-ary boundary).
    """
    chk = CheckResult(
        check_id="plausibility.patient_identity_stable",
        category="plausibility",
        subcategory="uniqueness",
        context="verification",
        threshold=threshold_for("plausibility.patient_identity_stable", thresholds),
        critical=False,
        description=(
            "No two Patient resources share an identifier system+value, and all "
            "Patient.link entries use a recognized reconciliation type."
        ),
        recommendation=(
            "Resolve duplicate identifiers or qualify Patient.link with a recognized "
            "type (replaced-by, replaces, refer, seealso). "
            "See HL7 EHR WG 2026 patient-reconciliation guidance."
        ),
        hdqt_category="plausibility",
        hdqt_dimension="situationally_implausible",
        resource_type="Patient",
    )

    patients = [
        r
        for r in resources
        if isinstance(r, dict) and r.get("resourceType") == "Patient"
    ]

    if len(patients) < 2:
        # Structural boundary: identity collision requires at least 2 patients.
        chk.skipped = True
        chk.skip_reason = (
            "fewer than 2 Patient resources in batch — n-ary check not applicable"
        )
        return chk

    # 1. Identifier collision: same (system, value) on two different patients.
    seen_identifiers: dict[tuple[str, str], str] = {}  # (sys, val) → patient id
    for pat in patients:
        pid = pat.get("id", "")
        for ident in pat.get("identifier", []):
            if not isinstance(ident, dict):
                continue
            sys = ident.get("system", "")
            val = ident.get("value", "")
            if not (sys and val):
                continue
            key = (sys, val)
            chk.applicable += 1
            if key in seen_identifiers and seen_identifiers[key] != pid:
                chk.violations += 1
                # `val` is a raw Patient identifier (MRN/SSN) — never persist it
                # raw in the passport; surface a stable non-reversible token.
                chk.add_detail(
                    resource_type="Patient",
                    resource_id=pid,
                    attribute="identifier",
                    hdqt_category="plausibility",
                    hdqt_dimension="situationally_implausible",
                    severity="warning",
                    detail=(
                        f"identifier {sys}|{redact_token(val)} also appears on "
                        f"Patient/{seen_identifiers[key]}"
                    ),
                )
            else:
                seen_identifiers[key] = pid

    # 2. Unqualified Patient.link (link present but type missing or unknown).
    for pat in patients:
        pid = pat.get("id", "")
        for link in pat.get("link", []):
            if not isinstance(link, dict):
                continue
            link_type = link.get("type", "")
            chk.applicable += 1
            if link_type not in _KNOWN_LINK_TYPES:
                chk.violations += 1
                chk.add_detail(
                    resource_type="Patient",
                    resource_id=pid,
                    attribute="link.type",
                    hdqt_category="plausibility",
                    hdqt_dimension="situationally_implausible",
                    severity="warning",
                    detail=(
                        f"Patient.link.type={link_type!r} is not a recognized "
                        "reconciliation type (replaced-by / replaces / refer / seealso)"
                    ),
                )

    return chk
