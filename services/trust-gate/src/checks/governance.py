"""Auditability evidence (fitness-for-use policy layer — NOT a Kahn DQ category).

Provenance presence is a sharing-readiness signal, not a data-quality measure, so
it is reported separately from the Kahn category scores and feeds the decision
policy (e.g. missing provenance caps a dataset at CONDITIONAL_PASS).

Phase 3B addition: PROVENANCE_PRESENT SAM — a scored CheckResult (Kahn conformance /
ISO HL7 21089 trust-anchor pattern) when TRUST_GATE_PROVENANCE_SEVERITY is set to
"block". Default is "warn" (advisory only, exposed via auditability dict).
"""

from __future__ import annotations

import logging
import os

from constants import REQUIRED_PROVENANCE_KEYS, threshold_for
from passport import CheckResult

_log = logging.getLogger("trust_gate.checks.governance")

# Resource types for which FHIR Provenance resources are clinically expected.
_EXPECTED_PROVENANCE_TARGETS = frozenset(
    {"Observation", "MedicationRequest", "DiagnosticReport", "Condition", "Procedure"}
)

# Severity mode: "warn" (advisory, not scored) | "block" (CheckResult, critical).
_PROVENANCE_SEVERITY = os.environ.get("TRUST_GATE_PROVENANCE_SEVERITY", "warn").lower()


def evaluate(resources: list[dict], provenance: dict | None) -> dict:
    provenance = provenance or {}
    missing = [k for k in REQUIRED_PROVENANCE_KEYS if not provenance.get(k)]

    meta_total = 0
    meta_with = 0
    for res in resources:
        if not isinstance(res, dict) or not res.get("resourceType"):
            continue
        meta_total += 1
        meta = res.get("meta")
        if isinstance(meta, dict) and (meta.get("lastUpdated") or meta.get("source")):
            meta_with += 1

    return {
        "provenance_present": not missing,
        "missing_provenance_keys": missing,
        "resource_meta_coverage": f"{meta_with}/{meta_total}" if meta_total else "0/0",
        "resource_meta_complete": meta_total > 0 and meta_with == meta_total,
    }


def evaluate_provenance_sam(
    resources: list[dict],
    thresholds: dict[str, float] | None = None,
) -> CheckResult | None:
    """PROVENANCE_PRESENT SAM (ISO HL7 21089 trust-anchor pattern).

    Checks that at least one ``Provenance`` resource exists in the batch
    targeting a critical clinical resource type. Only active when
    ``TRUST_GATE_PROVENANCE_SEVERITY=block``; returns None in warn-mode so the
    check stays out of the scored Kahn pass-rates.

    Returns a CheckResult (conformance / relational / validation) or None.
    """
    if _PROVENANCE_SEVERITY not in ("block", "score"):
        return None  # advisory only in warn mode — not a Kahn scored check

    chk = CheckResult(
        check_id="conformance.provenance_present",
        category="conformance",
        subcategory="relational",
        context="validation",
        threshold=threshold_for("conformance.provenance_present", thresholds),
        critical=(_PROVENANCE_SEVERITY == "block"),
        description=(
            "At least one Provenance resource targets a critical clinical resource "
            "(Observation, MedicationRequest, DiagnosticReport, Condition, Procedure)."
        ),
        recommendation=(
            "Include Provenance resources per ISO HL7 21089 trust-anchor requirements "
            "for regulated secondary use."
        ),
        hdqt_category="availability",
        hdqt_dimension="missing",
    )

    # Build a set of ids for resources that are expected to have provenance.
    expected_ids: set[str] = set()
    for res in resources:
        if not isinstance(res, dict):
            continue
        rt = res.get("resourceType")
        rid = res.get("id")
        if rt in _EXPECTED_PROVENANCE_TARGETS and rid:
            expected_ids.add(f"{rt}/{rid}")

    if not expected_ids:
        return chk  # NA — no expected-provenance resources in batch

    # Collect what Provenance targets.
    covered: set[str] = set()
    for res in resources:
        if not isinstance(res, dict) or res.get("resourceType") != "Provenance":
            continue
        for target in res.get("target", []):
            if isinstance(target, dict):
                ref = target.get("reference", "")
                # Normalize to "ResourceType/id" short form.
                short = "/".join(ref.split("?", 1)[0].rsplit("/", 2)[-2:])
                if short in expected_ids:
                    covered.add(short)

    chk.applicable = len(expected_ids)
    chk.violations = len(expected_ids - covered)
    return chk
