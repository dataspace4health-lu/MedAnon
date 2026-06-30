"""FHIR R4 -> OMOP CDM core (best-effort structural mapping).

Maps the common clinical resources onto the OMOP core so the same DQD-style
checks run over FHIR-sourced data. This is a STRUCTURAL mapping: coded values are
carried as source codes and standard ``*_concept_id`` columns are set to 0
(unmapped) because concept standardization needs a vocabulary the gate does not
host. The field/referential/range checks still apply; concept-standardness is out
of scope for the core cut.
"""

from __future__ import annotations

from cdm.omop_model import OmopData

# Administrative-gender string -> OMOP gender concept_id (the few well-known ones).
_GENDER_CONCEPT = {"male": 8507, "female": 8532, "other": 0, "unknown": 0}

_LAB_VITAL_CATEGORIES = {"laboratory", "vital-signs"}


def _subject_person_id(resource: dict) -> str | None:
    ref = (resource.get("subject") or {}).get("reference") or (
        resource.get("patient") or {}
    ).get("reference")
    if isinstance(ref, str) and "/" in ref:
        return ref.split("/", 1)[1]
    return ref if isinstance(ref, str) else None


def _date(value) -> str | None:
    return value[:10] if isinstance(value, str) and value else None


def _obs_is_measurement(resource: dict) -> bool:
    for cat in resource.get("category", []) or []:
        for coding in (cat or {}).get("coding", []) or []:
            if isinstance(coding, dict) and coding.get("code") in _LAB_VITAL_CATEGORIES:
                return True
    # An Observation carrying a numeric quantity is treated as a measurement.
    return "valueQuantity" in resource


def fhir_to_omop(resources: list[dict]) -> OmopData:
    omop = OmopData()
    for r in resources:
        if not isinstance(r, dict):
            continue
        rtype = r.get("resourceType")
        if rtype == "Patient":
            byear = None
            bd = r.get("birthDate")
            if isinstance(bd, str) and len(bd) >= 4 and bd[:4].isdigit():
                byear = int(bd[:4])
            omop.add("person", {
                "person_id": r.get("id"),
                "gender_concept_id": _GENDER_CONCEPT.get(r.get("gender"), 0),
                "year_of_birth": byear,
                "birth_datetime": bd,
            })
        elif rtype == "Observation":
            vq = r.get("valueQuantity") or {}
            row = {
                "person_id": _subject_person_id(r),
                "measurement_concept_id": 0,
                "measurement_source_value": _first_code(r.get("code")),
                "measurement_date": _date(r.get("effectiveDateTime")),
                "value_as_number": vq.get("value"),
                "unit_source_value": vq.get("unit") or vq.get("code"),
            }
            if _obs_is_measurement(r):
                row["measurement_id"] = r.get("id")
                omop.add("measurement", row)
            else:
                omop.add("observation", {
                    "observation_id": r.get("id"),
                    "person_id": row["person_id"],
                    "observation_concept_id": 0,
                    "observation_source_value": row["measurement_source_value"],
                    "observation_date": row["measurement_date"],
                })
        elif rtype == "Condition":
            omop.add("condition_occurrence", {
                "condition_occurrence_id": r.get("id"),
                "person_id": _subject_person_id(r),
                "condition_concept_id": 0,
                "condition_source_value": _first_code(r.get("code")),
                "condition_start_date": _date(
                    r.get("onsetDateTime") or r.get("recordedDate")
                ),
            })
        elif rtype in ("MedicationRequest", "MedicationStatement"):
            omop.add("drug_exposure", {
                "drug_exposure_id": r.get("id"),
                "person_id": _subject_person_id(r),
                "drug_concept_id": 0,
                "drug_source_value": _first_code(r.get("medicationCodeableConcept")),
                "drug_exposure_start_date": _date(
                    r.get("authoredOn")
                    or (r.get("effectivePeriod") or {}).get("start")
                ),
            })
        elif rtype == "Encounter":
            period = r.get("period") or {}
            omop.add("visit_occurrence", {
                "visit_occurrence_id": r.get("id"),
                "person_id": _subject_person_id(r),
                "visit_concept_id": 0,
                "visit_start_date": _date(period.get("start")),
                "visit_end_date": _date(period.get("end")),
            })
        elif rtype == "Procedure":
            omop.add("procedure_occurrence", {
                "procedure_occurrence_id": r.get("id"),
                "person_id": _subject_person_id(r),
                "procedure_concept_id": 0,
                "procedure_source_value": _first_code(r.get("code")),
                "procedure_date": _date(
                    r.get("performedDateTime")
                    or (r.get("performedPeriod") or {}).get("start")
                ),
            })
    return omop


def _first_code(codeable) -> str | None:
    for coding in (codeable or {}).get("coding", []) or []:
        if isinstance(coding, dict) and coding.get("code"):
            return f"{coding.get('system', '')}|{coding['code']}"
    return None
