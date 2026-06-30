"""Apply the solver's GeneralizationPlan to de-identified resources.

Operates on *already-rule-processed* resources (i.e. after
``process_data_batch``).  For Patient resources, QI fields are overwritten
with the solver-chosen generalization level.  Records whose patient id
appears in ``plan.suppressed_ids`` are dropped.

Order of operations rationale
------------------------------
The normal rule-driven pipeline runs first (gPAS pseudonymization, NLP
scrubbing, redactions, etc.).  ``apply_plan`` then overwrites the QI paths
with the solver's chosen level.  This ensures the privacy guarantee is
enforced *last* and cannot be accidentally undone by a later rule.  For
non-Patient resources, ``apply_plan`` is a no-op unless ``suppress_linked``
is enabled and the patient is suppressed.

Linked-resource suppression
----------------------------
When ``privacy_model.suppress_linked=true`` (default), non-Patient resources
whose ``subject.reference`` resolves to a suppressed Patient are also dropped.
This prevents re-identification through linked Conditions, Observations, etc.
The mapping ``subject.reference → Patient id`` is the same normalisation used
by ``analytics.risk.build_conditions_map``.

Public API
----------
    apply_plan(resource, plan, privacy_model) -> dict | None
        Returns the (possibly modified) resource, or None if suppressed.

    filter_and_apply(resources, plan, privacy_model) -> list[dict]
        Convenience wrapper that filters and transforms a list in one call.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.privacy.lattice import GeneralizationPlan

_log = logging.getLogger("medanon.privacy.apply")

# Manifest system URL — reuse the constant from pipeline.manifest.
_MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest"
_SUPPRESSED_SYSTEM = "https://medanon.local/privacy-suppressed"


def _set_field(resource: dict, fhir_path: str, value: str) -> None:
    """Overwrite a scalar FHIR path in place (best-effort, does not add paths).

    Supports the two-level patterns used for QIs:
      Patient.birthDate, Patient.gender, Patient.address.postalCode.
    """
    parts = fhir_path.split(".")
    # Strip resource-type prefix
    if len(parts) > 1 and parts[0][0].isupper():
        parts = parts[1:]

    obj = resource
    for part in parts[:-1]:
        val = obj.get(part)
        if isinstance(val, list):
            val = val[0] if val else None
        if not isinstance(val, dict):
            return  # path doesn't exist — skip silently
        obj = val

    last = parts[-1]
    if last in obj:
        if isinstance(obj[last], list):
            # Overwrite first list element
            if obj[last]:
                obj[last][0] = value
        else:
            obj[last] = value


def _get_patient_id(resource: dict) -> str:
    """Extract the Patient.id from a Patient resource."""
    return str(resource.get("id") or "").strip()


def _get_subject_patient_id(resource: dict) -> str:
    """Extract the patient id from subject.reference ('Patient/abc' → 'abc')."""
    subject = resource.get("subject") or {}
    ref = str(subject.get("reference") or "").strip()
    if not ref:
        return ""
    return ref.split("/")[-1]


def _attach_suppressed_tag(resource: dict) -> None:
    """Add a meta.tag marker to a resource indicating it was suppressed."""
    meta = resource.setdefault("meta", {})
    tags = meta.setdefault("tag", [])
    tags.append(
        {
            "system": _SUPPRESSED_SYSTEM,
            "code": "patient-suppressed",
            "display": "Suppressed for k-anonymity guarantee",
        }
    )


def _emit_suppression_audit(resource: dict, patient_id: str, reason: str) -> None:
    """Emit a Prometheus counter + INFO log for one suppressed resource.

    The patient ID is one-way hashed (SHA-256, first 16 hex chars) so audit
    events carry a stable correlation key without embedding raw identifiers.
    """
    rtype = resource.get("resourceType", "unknown")
    rid = resource.get("id", "")
    pid_hash = hashlib.sha256(patient_id.encode()).hexdigest()[:16]

    _log.info(
        "k_anonymity_suppress rtype=%s id=%s patient_sha256_prefix=%s reason=%s",
        rtype,
        rid,
        pid_hash,
        reason,
    )

    try:
        from utils.metrics import RESOURCES_SUPPRESSED

        RESOURCES_SUPPRESSED.labels(resource_type=rtype, reason=reason).inc()
    except Exception:  # noqa: BLE001 — metrics must never break the pipeline
        pass

    try:
        from utils.audit import emit as audit_emit

        audit_emit(
            "privacy.suppress",
            resource_type=rtype,
            resource_id=rid,
            action=reason,
            outcome="success",
            detail={"patient_id_sha256_prefix": pid_hash},
        )
    except Exception:  # noqa: BLE001 — audit must never break the pipeline
        pass


def apply_plan(
    resource: dict,
    plan: "GeneralizationPlan",
    privacy_model: dict,
) -> dict | None:
    """Apply *plan* to a single *resource*.

    Returns
    -------
    dict | None
        The (possibly modified) resource, or ``None`` if the resource should
        be dropped (suppressed).
    """
    rtype = resource.get("resourceType", "")
    suppress_linked: bool = privacy_model.get("suppress_linked", True)

    if rtype == "Patient":
        pid = _get_patient_id(resource)
        if pid and pid in plan.suppressed_ids:
            _attach_suppressed_tag(resource)
            _emit_suppression_audit(resource, pid, "k_anonymity")
            return None
        # Overwrite QI fields with the solver-chosen generalization level.
        from pipeline.privacy.hierarchies import level_value

        for qi_path, qi_kind in zip(
            plan.levels.keys(), _iter_kinds(plan, privacy_model)
        ):
            lvl = plan.levels[qi_path]
            if lvl == 0:
                continue  # level 0 = identity; no change needed
            original = _get_field_value(resource, qi_path)
            if original:
                generalised = level_value(qi_kind, lvl, original)
                _set_field(resource, qi_path, generalised)
        return resource

    elif suppress_linked and plan.suppressed_ids:
        # Check if this non-Patient resource is linked to a suppressed Patient.
        pid = _get_subject_patient_id(resource)
        if pid and pid in plan.suppressed_ids:
            _attach_suppressed_tag(resource)
            _emit_suppression_audit(resource, pid, "k_anonymity_linked")
            return None

    return resource


def filter_and_apply(
    resources: list[dict],
    plan: "GeneralizationPlan",
    privacy_model: dict,
) -> list[dict]:
    """Apply *plan* to a list of resources, returning only non-suppressed ones."""
    result = []
    for r in resources:
        out = apply_plan(r, plan, privacy_model)
        if out is not None:
            result.append(out)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_field_value(resource: dict, fhir_path: str) -> str:
    """Read a scalar field value for the purpose of generalising it."""
    parts = fhir_path.split(".")
    if len(parts) > 1 and parts[0][0].isupper():
        parts = parts[1:]
    obj = resource
    for part in parts:
        if isinstance(obj, list):
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
    return str(obj).strip()


def _iter_kinds(plan: "GeneralizationPlan", privacy_model: dict):
    """Yield the kind string for each QI path in plan.levels order."""
    kind_map = {
        q["path"]: q["kind"] for q in privacy_model.get("quasi_identifiers", [])
    }
    for p in plan.levels:
        yield kind_map.get(p, "category")
