"""Out-of-core quasi-identifier equivalence-class index.

Builds a compact in-memory representation of the QI distribution for a full
dataset without holding all resources simultaneously.  Designed to consume a
keyset-paginated iterator (e.g. ``StagingStore.get_all_resources``) so the
bounded-memory guarantee of the streaming pipeline is maintained.

Key design choices
------------------
- Only *Patient* resources contribute QI tuples; all other resource types are
  recorded only for linked-resource suppression (``subject.reference``).
- The QI tuple is a simple ``tuple[str, ...]`` of *raw* (un-generalised) field
  values, one entry per declared ``quasi_identifier`` in the privacy model.
  Generalisation is applied by the lattice solver, not here, so the index can
  re-bucket cheaply for any candidate generalization-level vector.
- For l-diversity: Condition codes are linked to Patients via
  ``subject.reference`` (reuses ``analytics.risk.build_conditions_map``).
- Reservoir sampling (``MEDANON_KANON_MAX_PATIENTS`` default 200 000) bounds
  memory for extreme datasets; the reservoir is exact for datasets below the cap.

Public API
----------
    index = build_qi_index(staged_iter, privacy_model)
    index.patient_qi_tuples   # list[tuple[str, ...]]  (raw values, not yet generalised)
    index.patient_ids         # list[str]              (parallel to qi_tuples)
    index.conditions_by_patient  # dict[str, set[str]] (for l-diversity)
    index.total_patients      # int
    index.total_resources     # int
"""

from __future__ import annotations

import logging
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

_log = logging.getLogger("medanon.privacy.qi_index")

_MAX_PATIENTS = int(os.environ.get("MEDANON_KANON_MAX_PATIENTS", "200000"))

# ---------------------------------------------------------------------------
# QI extraction helpers
# ---------------------------------------------------------------------------

def _extract_field_value(resource: dict, fhir_path: str) -> str:
    """Extract a single scalar QI value from a resource using a simple path.

    Supports only two-level paths like ``Patient.birthDate`` and
    ``Patient.address.postalCode`` — the common QI patterns.  For
    ``address.postalCode``, the first address entry is used.

    Returns an empty string when the field is absent.
    """
    parts = fhir_path.split(".")
    # Strip resource type prefix (e.g. "Patient")
    if len(parts) > 1 and parts[0][0].isupper():
        parts = parts[1:]

    obj = resource
    for i, part in enumerate(parts):
        if isinstance(obj, list):
            # Take first non-empty list item
            obj = obj[0] if obj else None
        if not isinstance(obj, dict):
            return ""
        obj = obj.get(part)
        if obj is None:
            return ""

    if isinstance(obj, list):
        obj = obj[0] if obj else None

    if obj is None:
        return ""
    if isinstance(obj, dict):
        # e.g. CodeableConcept — take text or first coding.code
        codings = obj.get("coding") or []
        if codings and isinstance(codings, list):
            code = (codings[0].get("code") or codings[0].get("display") or "")
            return str(code).strip()
        return str(obj.get("text") or "").strip()
    return str(obj).strip()


def _extract_condition_code(resource: dict) -> str:
    """Extract the primary code string from a Condition resource."""
    code_obj = resource.get("code") or {}
    codings = code_obj.get("coding") or []
    if codings and isinstance(codings, list):
        code = str(codings[0].get("code") or codings[0].get("display") or "")
        if code:
            return code.strip()
    return str(code_obj.get("text") or "").strip()


def _extract_subject_patient_id(resource: dict) -> str:
    """Return the Patient id from subject.reference ('Patient/abc' → 'abc')."""
    subject = resource.get("subject") or {}
    ref = str(subject.get("reference") or "").strip()
    if not ref:
        return ""
    return ref.split("/")[-1]


# ---------------------------------------------------------------------------
# QiIndex dataclass
# ---------------------------------------------------------------------------

@dataclass
class QiIndex:
    """Compact QI index for a full dataset.

    Attributes
    ----------
    qi_paths        Ordered list of FHIRPath strings for the QI fields.
    qi_kinds        Ordered list of hierarchy kinds (parallel to qi_paths).
    patient_ids     Patient id strings (order matches qi_tuples).
    patient_qi_tuples  Raw (un-generalised) QI values per patient.
    conditions_by_patient  patient_id → set of Condition code strings.
    total_patients  Total Patients seen (may exceed len(patient_ids) if capped).
    total_resources Total resources seen (all types).
    sampled         True if reservoir sampling was applied (dataset exceeded cap).
    """
    qi_paths: list[str] = field(default_factory=list)
    qi_kinds: list[str] = field(default_factory=list)
    patient_ids: list[str] = field(default_factory=list)
    patient_qi_tuples: list[tuple[str, ...]] = field(default_factory=list)
    conditions_by_patient: dict[str, set[str]] = field(default_factory=dict)
    total_patients: int = 0
    total_resources: int = 0
    sampled: bool = False


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_qi_index(
    staged_iter: Iterable[dict],
    privacy_model: dict,
    *,
    max_patients: int = _MAX_PATIENTS,
) -> QiIndex:
    """Scan *staged_iter* once and build a compact QiIndex.

    Parameters
    ----------
    staged_iter:
        An iterable of staging-row dicts (each has a ``resource_json`` key
        that is either a ``dict`` or a JSON string), or plain resource dicts.
    privacy_model:
        The validated privacy_model dict from ``Settings.privacy_model``.
    max_patients:
        Reservoir cap.  Overridden by ``MEDANON_KANON_MAX_PATIENTS`` env var
        at module load; explicit parameter is for testing.
    """
    from utils.json_fast import loads as _json_loads

    qis = privacy_model.get("quasi_identifiers", [])
    qi_paths = [q["path"] for q in qis]
    qi_kinds = [q["kind"] for q in qis]

    # Accumulators
    patient_ids: list[str] = []
    patient_qi_tuples: list[tuple[str, ...]] = []
    conditions_by_patient: dict[str, set[str]] = defaultdict(set)
    total_patients = 0
    total_resources = 0
    sampled = False

    # Reservoir sampling state (Vitter's Algorithm R)
    reservoir_size = max_patients
    # We fill up to max_patients exactly; after that, replace with probability
    # reservoir_size / (total_patients + 1).
    rng = random.Random(42)  # deterministic seed for reproducibility

    for row in staged_iter:
        total_resources += 1

        # Accept plain FHIR resource dicts only.
        # Staging rows no longer contain resource_json — they hold only references
        # (resource_id, resource_type, fhir_source_url).  The caller is responsible
        # for re-fetching resources from FHIR before passing them here.
        if not isinstance(row, dict):
            continue
        resource = row

        if not isinstance(resource, dict):
            continue

        rtype = resource.get("resourceType", "")

        if rtype == "Patient":
            total_patients += 1
            pid = str(resource.get("id") or "")
            qi_values = tuple(
                _extract_field_value(resource, path) for path in qi_paths
            )

            if len(patient_ids) < reservoir_size:
                patient_ids.append(pid)
                patient_qi_tuples.append(qi_values)
            else:
                # Reservoir replacement
                sampled = True
                j = rng.randint(0, total_patients - 1)
                if j < reservoir_size:
                    # Also update conditions for replaced entry: remove old,
                    # but we can't easily do that without the old id.  In
                    # practice the conditions_by_patient dict grows beyond the
                    # reservoir — acceptable since Condition count is bounded.
                    patient_ids[j] = pid
                    patient_qi_tuples[j] = qi_values

        elif rtype == "Condition":
            code = _extract_condition_code(resource)
            if code:
                pid = _extract_subject_patient_id(resource)
                if pid:
                    conditions_by_patient[pid].add(code)

        if total_resources % 10_000 == 0:
            _log.debug(
                "qi_index_scan resources=%d patients=%d (sampled=%s)",
                total_resources,
                total_patients,
                sampled,
            )

    _log.info(
        "qi_index_built total_resources=%d total_patients=%d "
        "index_size=%d sampled=%s conditions_patients=%d",
        total_resources,
        total_patients,
        len(patient_ids),
        sampled,
        len(conditions_by_patient),
    )

    return QiIndex(
        qi_paths=qi_paths,
        qi_kinds=qi_kinds,
        patient_ids=patient_ids,
        patient_qi_tuples=patient_qi_tuples,
        conditions_by_patient=dict(conditions_by_patient),
        total_patients=total_patients,
        total_resources=total_resources,
        sampled=sampled,
    )
