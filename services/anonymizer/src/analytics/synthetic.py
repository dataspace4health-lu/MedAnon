"""Synthetic FHIR resource generator — statistical synthesis from de-identified data.

Generates synthetic FHIR Patient and Condition resources that preserve the
statistical distributions of the input dataset without copying any real
patient records.  No new dependencies — stdlib only.

Usage
-----
    from analytics.synthetic import generate_synthetic_patients
    patients = [...]          # list of de-identified FHIR Patient dicts
    synthetic = generate_synthetic_patients(patients, count=100, seed=42)

    # Optional: generate correlated Conditions
    from analytics.synthetic import generate_synthetic_conditions
    conditions = [...]        # list of de-identified FHIR Condition dicts
    syn_conditions = generate_synthetic_conditions(
        conditions, synthetic, count_per_patient=2, seed=42
    )

The output resources are tagged with the ``SYN`` observation value code so
downstream systems can distinguish synthetic from real de-identified data.
"""

from __future__ import annotations

import random
import uuid
from typing import Any


_SYN_TAG = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
    "code": "SYN",
    "display": "synthetic",
}


# ---------------------------------------------------------------------------
# Distribution extraction
# ---------------------------------------------------------------------------


def _extract_distributions(
    patients: list[dict],
) -> dict[str, list[str]]:
    """Extract per-attribute value lists from *patients* for weighted sampling.

    Each list has one entry per patient so ``random.choice()`` automatically
    preserves the input frequency distribution.
    """
    genders: list[str] = []
    birth_years: list[str] = []
    zip_prefixes: list[str] = []
    marital_statuses: list[str] = []
    languages: list[str] = []

    for p in patients:
        genders.append((p.get("gender") or "").strip().lower())

        birth_date = (p.get("birthDate") or "").strip()
        birth_years.append(birth_date[:4] if len(birth_date) >= 4 else birth_date)

        addresses = p.get("address") or []
        postal = ""
        if isinstance(addresses, list) and addresses:
            postal = str(addresses[0].get("postalCode") or "")
        zip_prefixes.append(postal[:3])

        # maritalStatus
        ms = p.get("maritalStatus")
        ms_code = ""
        if isinstance(ms, dict):
            codings = ms.get("coding") or []
            if isinstance(codings, list) and codings:
                ms_code = str(codings[0].get("code") or "")
        marital_statuses.append(ms_code)

        # communication.language
        comms = p.get("communication") or []
        lang = ""
        if isinstance(comms, list) and comms:
            lang_obj = comms[0].get("language") or {}
            if isinstance(lang_obj, dict):
                lang_codings = lang_obj.get("coding") or []
                if isinstance(lang_codings, list) and lang_codings:
                    lang = str(lang_codings[0].get("code") or "")
        languages.append(lang)

    return {
        "genders": genders,
        "birth_years": birth_years,
        "zip_prefixes": zip_prefixes,
        "marital_statuses": marital_statuses,
        "languages": languages,
    }


# ---------------------------------------------------------------------------
# Synthetic patient builder
# ---------------------------------------------------------------------------


def _make_patient(
    rng: random.Random,
    dists: dict[str, list[str]],
) -> dict[str, Any]:
    """Build one synthetic FHIR Patient resource."""
    gender = rng.choice(dists["genders"])

    # Sample birth year and apply ±2-year jitter for diversity
    raw_year = rng.choice(dists["birth_years"])
    if raw_year.isdigit() and len(raw_year) == 4:
        jittered = rng.randint(int(raw_year) - 2, int(raw_year) + 2)
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        birth_date = f"{jittered}-{month:02d}-{day:02d}"
    else:
        birth_date = raw_year

    zip_prefix = rng.choice(dists["zip_prefixes"])
    postal_code = f"{zip_prefix}000" if zip_prefix else ""

    marital = rng.choice(dists["marital_statuses"])
    language = rng.choice(dists["languages"])

    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "meta": {"tag": [_SYN_TAG.copy()]},
    }

    if gender:
        resource["gender"] = gender

    if birth_date:
        resource["birthDate"] = birth_date

    if postal_code:
        resource["address"] = [{"postalCode": postal_code}]

    if marital:
        resource["maritalStatus"] = {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus",
                    "code": marital,
                }
            ],
        }

    if language:
        resource["communication"] = [
            {
                "language": {
                    "coding": [{"system": "urn:ietf:bcp:47", "code": language}],
                },
            }
        ]

    return resource


# ---------------------------------------------------------------------------
# Condition distribution extraction & builder
# ---------------------------------------------------------------------------


def _extract_condition_distributions(
    conditions: list[dict],
) -> dict[str, list]:
    """Extract distributions from real Condition resources."""
    codes: list[dict] = []
    clinical_statuses: list[str] = []
    categories: list[dict] = []

    for c in conditions:
        # code (the diagnosis)
        code_obj = c.get("code")
        if isinstance(code_obj, dict):
            codes.append(code_obj)
        else:
            codes.append({"text": "Unknown"})

        # clinicalStatus
        cs = c.get("clinicalStatus")
        cs_code = "active"
        if isinstance(cs, dict):
            cs_codings = cs.get("coding") or []
            if isinstance(cs_codings, list) and cs_codings:
                cs_code = str(cs_codings[0].get("code") or "active")
        clinical_statuses.append(cs_code)

        # category
        cats = c.get("category") or []
        if isinstance(cats, list) and cats:
            categories.append(cats[0])
        else:
            categories.append(
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                            "code": "encounter-diagnosis",
                        }
                    ],
                }
            )

    return {
        "codes": codes,
        "clinical_statuses": clinical_statuses,
        "categories": categories,
    }


def _make_condition(
    rng: random.Random,
    patient_id: str,
    dists: dict[str, list],
) -> dict[str, Any]:
    """Build one synthetic FHIR Condition resource linked to a Patient."""
    import copy

    code = copy.deepcopy(rng.choice(dists["codes"]))
    clinical_status = rng.choice(dists["clinical_statuses"])
    category = copy.deepcopy(rng.choice(dists["categories"]))

    return {
        "resourceType": "Condition",
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "meta": {"tag": [_SYN_TAG.copy()]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "code": code,
        "clinicalStatus": {
            "coding": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                    "code": clinical_status,
                }
            ],
        },
        "category": [category],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_synthetic_patients(
    patients: list[dict],
    count: int,
    seed: int | None = None,
) -> list[dict]:
    """Generate *count* synthetic FHIR Patient resources from *patients*.

    Statistical distributions for gender, birth year, zip prefix, marital
    status, and language are sampled from the input.  Each synthetic record
    receives a new UUID and is tagged with the ``SYN`` code.

    Args:
        patients: List of de-identified FHIR Patient dicts.  Must contain at
                  least one record.
        count: Number of synthetic patients to generate (1 – 10 000).
        seed: Optional random seed for reproducibility.

    Returns:
        List of *count* synthetic FHIR Patient dicts.

    Raises:
        ValueError: If *patients* is empty or *count* is out of range.
    """
    if not patients:
        raise ValueError(
            "patients list must not be empty — no distribution to sample from"
        )
    if count < 1 or count > 10_000:
        raise ValueError(f"count must be between 1 and 10 000, got {count}")

    rng = random.Random(seed)
    dists = _extract_distributions(patients)

    return [_make_patient(rng, dists) for _ in range(count)]


def generate_synthetic_conditions(
    conditions: list[dict],
    synthetic_patients: list[dict],
    count_per_patient: int = 2,
    seed: int | None = None,
) -> list[dict]:
    """Generate synthetic Condition resources linked to *synthetic_patients*.

    Each synthetic patient receives up to *count_per_patient* Conditions,
    sampled from the diagnosis code, clinical status, and category
    distributions in the input *conditions* list.

    Args:
        conditions: List of real/de-identified FHIR Condition dicts.
        synthetic_patients: List of synthetic Patient dicts (output of
                            ``generate_synthetic_patients``).
        count_per_patient: Max conditions per patient (actual count is random 0–N).
        seed: Optional random seed for reproducibility.

    Returns:
        List of synthetic FHIR Condition dicts.

    Raises:
        ValueError: If *conditions* or *synthetic_patients* is empty.
    """
    if not conditions:
        raise ValueError("conditions list must not be empty")
    if not synthetic_patients:
        raise ValueError("synthetic_patients list must not be empty")

    rng = random.Random(seed)
    dists = _extract_condition_distributions(conditions)

    result: list[dict] = []
    for patient in synthetic_patients:
        n = rng.randint(0, count_per_patient)
        for _ in range(n):
            result.append(_make_condition(rng, patient["id"], dists))
    return result
