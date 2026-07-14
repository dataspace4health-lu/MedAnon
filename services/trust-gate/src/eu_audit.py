"""GDPR-aligned processing record for the read-only QC evaluation (EU compliance).

The Trust Gate is a READ-ONLY evaluator: it reads a dataset, assesses quality, and
emits a Quality Passport. It never transforms, pseudonymizes, or writes back data
(that is the separate anonymizer service). This module assembles a GDPR Art. 30
"record of processing activities" view over a stored assessment + its ALCOA++ audit
trail, so a data holder can attach an EU-audit-ready record to the QC report.

Derived purely from an already-computed passport + audit trail  adds no scoring
and persists nothing new.
"""

from __future__ import annotations

RECORD_SCHEME = "GDPR Reg. (EU) 2016/679 Art. 30 record of processing activities"


def build_processing_record(
    passport: dict, audit_trail: list[dict] | None = None
) -> dict:
    """Assemble the GDPR/EHDS processing record for a read-only QC evaluation."""
    ev = passport.get("evaluation") or {}
    prov = passport.get("provenance") or {}
    fw_versions = passport.get("framework_versions") or {}
    return {
        "record_scheme": RECORD_SCHEME,
        "regulation_basis": [
            "GDPR Reg. (EU) 2016/679 Art. 30",
            "EHDS Reg. (EU) 2025/327",
        ],
        # The defining property of this service for an EU audit: it does not act on
        # the data. Stated explicitly so the record is unambiguous.
        "processing_nature": (
            "read-only data-quality evaluation: the service reads and evaluates the "
            "dataset and produces a quality passport; it does NOT transform, "
            "pseudonymize, anonymize, or write back any data"
        ),
        "data_acted_upon": False,
        "purpose": (
            "data-quality and fitness-for-secondary-use evaluation "
            "(EHDS Art. 56 quality & utility label)"
        ),
        "controller_role": ev.get("org_role", "data-receiving"),
        "lifecycle_stage": ev.get("lifecycle_stage", "operation"),
        "data_categories": (
            "FHIR/OMOP clinical resources, evaluated in memory; only PHI-safe defect "
            "locations are retained (resource + field + reason), never raw values"
        ),
        # GDPR data-minimisation evidence: the passport never persists raw PHI.
        "phi_handling": {
            "raw_values_persisted": False,
            "identifier_values": "tokenized (sha256), never stored raw",
        },
        "dataset_id": passport.get("dataset_id"),
        "data_source": {
            "source_system": prov.get("source_system"),
            "extraction_time": prov.get("extraction_time"),
        },
        "assessment": {
            "decision": passport.get("decision"),
            "generated_at": passport.get("generated_at"),
            "framework": passport.get("framework"),
            "framework_versions": fw_versions,
            # Reproducibility is the EU-audit anchor: a deterministic, seeded verdict
            # can be independently re-derived from the same input.
            "reproducibility": {
                "decision_basis": ev.get("decision_basis"),
                "sampler_seed": ev.get("sampler_seed"),
                "deterministic": ev.get("decision_basis") == "deterministic",
            },
        },
        # ALCOA++ (Attributable, Legible, Contemporaneous, Original, Accurate, +
        # Complete, Consistent, Enduring, Available): the append-only assessment trail.
        "alcoa_plus_trail": audit_trail or [],
    }
