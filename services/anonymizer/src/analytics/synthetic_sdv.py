"""SDV-powered synthetic FHIR resource generator.

Uses the Synthetic Data Vault (SDV) library with GaussianCopulaSynthesizer to
generate synthetic FHIR Patient and Condition resources that preserve the
multivariate correlations of the input dataset.

This module is optional — it requires the ``sdv`` package (and its transitive
dependencies: scipy, numpy, pandas, etc.).  When SDV is not installed, callers
should fall back to the stdlib-based generator in ``analytics.synthetic``.

Usage
-----
    from analytics.synthetic_sdv import SDV_AVAILABLE

    if SDV_AVAILABLE:
        from analytics.synthetic_sdv import (
            generate_synthetic_patients_sdv,
            generate_synthetic_conditions_sdv,
        )
        patients = generate_synthetic_patients_sdv(input_patients, count=100, seed=42)
"""
from __future__ import annotations

import uuid
from typing import Any

try:
    import pandas as pd
    from sdv.single_table import GaussianCopulaSynthesizer
    from sdv.metadata import Metadata

    SDV_AVAILABLE = True
except ImportError:
    SDV_AVAILABLE = False

_SYN_TAG = {
    "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
    "code": "SYN",
    "display": "synthetic",
}


# ---------------------------------------------------------------------------
# Flatten / unflatten helpers
# ---------------------------------------------------------------------------

def _flatten_patient(p: dict) -> dict[str, str]:
    """Extract key attributes from a FHIR Patient into a flat dict for SDV."""
    row: dict[str, str] = {}

    row["gender"] = (p.get("gender") or "").strip().lower()

    birth_date = (p.get("birthDate") or "").strip()
    row["birth_year"] = birth_date[:4] if len(birth_date) >= 4 else birth_date

    addresses = p.get("address") or []
    postal = ""
    if isinstance(addresses, list) and addresses:
        postal = str(addresses[0].get("postalCode") or "")
    row["zip_prefix"] = postal[:3]

    ms = p.get("maritalStatus")
    ms_code = ""
    if isinstance(ms, dict):
        codings = ms.get("coding") or []
        if isinstance(codings, list) and codings:
            ms_code = str(codings[0].get("code") or "")
    row["marital_status"] = ms_code

    comms = p.get("communication") or []
    lang = ""
    if isinstance(comms, list) and comms:
        lang_obj = comms[0].get("language") or {}
        if isinstance(lang_obj, dict):
            lang_codings = lang_obj.get("coding") or []
            if isinstance(lang_codings, list) and lang_codings:
                lang = str(lang_codings[0].get("code") or "")
    row["language"] = lang

    return row


def _unflatten_patient(row: dict, rng) -> dict[str, Any]:
    """Convert a flat SDV-generated row back into a FHIR Patient resource."""
    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "meta": {"tag": [_SYN_TAG.copy()]},
    }

    gender = str(row.get("gender", "")).strip()
    if gender:
        resource["gender"] = gender

    year = str(row.get("birth_year", "")).strip()
    if year and year.isdigit() and len(year) == 4:
        month = rng.randint(1, 12)
        day = rng.randint(1, 28)
        resource["birthDate"] = f"{year}-{month:02d}-{day:02d}"

    zip_prefix = str(row.get("zip_prefix", "")).strip()
    if zip_prefix:
        resource["address"] = [{"postalCode": f"{zip_prefix}000"}]

    marital = str(row.get("marital_status", "")).strip()
    if marital:
        resource["maritalStatus"] = {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": marital}],
        }

    language = str(row.get("language", "")).strip()
    if language:
        resource["communication"] = [{
            "language": {
                "coding": [{"system": "urn:ietf:bcp:47", "code": language}],
            },
        }]

    return resource


def _flatten_condition(c: dict) -> dict[str, str]:
    """Extract key attributes from a FHIR Condition into a flat dict for SDV."""
    row: dict[str, str] = {}

    code_obj = c.get("code")
    if isinstance(code_obj, dict):
        codings = code_obj.get("coding") or []
        if isinstance(codings, list) and codings:
            row["diagnosis_code"] = str(codings[0].get("code") or "")
            row["diagnosis_display"] = str(codings[0].get("display") or "")
            row["diagnosis_system"] = str(codings[0].get("system") or "")
        else:
            row["diagnosis_code"] = code_obj.get("text", "Unknown")
            row["diagnosis_display"] = ""
            row["diagnosis_system"] = ""
    else:
        row["diagnosis_code"] = "Unknown"
        row["diagnosis_display"] = ""
        row["diagnosis_system"] = ""

    cs = c.get("clinicalStatus")
    cs_code = "active"
    if isinstance(cs, dict):
        cs_codings = cs.get("coding") or []
        if isinstance(cs_codings, list) and cs_codings:
            cs_code = str(cs_codings[0].get("code") or "active")
    row["clinical_status"] = cs_code

    cats = c.get("category") or []
    cat_code = "encounter-diagnosis"
    if isinstance(cats, list) and cats:
        cat_codings = cats[0].get("coding") or []
        if isinstance(cat_codings, list) and cat_codings:
            cat_code = str(cat_codings[0].get("code") or "encounter-diagnosis")
    row["category"] = cat_code

    return row


def _unflatten_condition(row: dict, patient_id: str, rng) -> dict[str, Any]:
    """Convert a flat SDV-generated row back into a FHIR Condition resource."""
    diag_code = str(row.get("diagnosis_code", "Unknown")).strip()
    diag_display = str(row.get("diagnosis_display", "")).strip()
    diag_system = str(row.get("diagnosis_system", "")).strip()

    code_obj: dict[str, Any]
    if diag_system:
        coding_entry: dict[str, str] = {"system": diag_system, "code": diag_code}
        if diag_display:
            coding_entry["display"] = diag_display
        code_obj = {"coding": [coding_entry]}
    else:
        code_obj = {"text": diag_code}

    clinical_status = str(row.get("clinical_status", "active")).strip()
    category = str(row.get("category", "encounter-diagnosis")).strip()

    return {
        "resourceType": "Condition",
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "meta": {"tag": [_SYN_TAG.copy()]},
        "subject": {"reference": f"Patient/{patient_id}"},
        "code": code_obj,
        "clinicalStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": clinical_status,
            }],
        },
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                "code": category,
            }],
        }],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_synthetic_patients_sdv(
    patients: list[dict],
    count: int,
    seed: int | None = None,
) -> list[dict]:
    """Generate *count* synthetic FHIR Patient resources using SDV GaussianCopula.

    The GaussianCopulaSynthesizer learns multivariate correlations between
    attributes (gender, birth year, zip prefix, marital status, language) and
    generates new rows that preserve those correlations — a significant
    improvement over independent per-attribute sampling.

    Args:
        patients: List of de-identified FHIR Patient dicts (>= 1 record).
        count: Number of synthetic patients to generate (1–10 000).
        seed: Optional random seed for reproducibility.

    Returns:
        List of *count* synthetic FHIR Patient dicts, tagged ``SYN``.

    Raises:
        ValueError: If *patients* is empty or *count* is out of range.
        RuntimeError: If SDV is not installed.
    """
    if not SDV_AVAILABLE:
        raise RuntimeError(
            "SDV is not installed. Install with: pip install -r requirements-sdv.txt"
        )
    if not patients:
        raise ValueError("patients list must not be empty — no distribution to sample from")
    if count < 1 or count > 10_000:
        raise ValueError(f"count must be between 1 and 10 000, got {count}")

    import random
    rng = random.Random(seed)

    rows = [_flatten_patient(p) for p in patients]
    df = pd.DataFrame(rows)

    # All columns are categorical for FHIR demographics.
    metadata = Metadata()
    metadata.add_table("patients")
    for col in df.columns:
        metadata.update_column("patients", col, sdtype="categorical")

    synthesizer = GaussianCopulaSynthesizer(
        metadata,
        enforce_min_max_values=False,
        enforce_rounding=False,
    )
    synthesizer.fit(df)

    synthetic_df = synthesizer.sample(num_rows=count)

    return [
        _unflatten_patient(row, rng)
        for row in synthetic_df.to_dict(orient="records")
    ]


def generate_synthetic_conditions_sdv(
    conditions: list[dict],
    synthetic_patients: list[dict],
    count_per_patient: int = 2,
    seed: int | None = None,
) -> list[dict]:
    """Generate synthetic Condition resources linked to *synthetic_patients* using SDV.

    Args:
        conditions: List of real/de-identified FHIR Condition dicts.
        synthetic_patients: List of synthetic Patient dicts.
        count_per_patient: Max conditions per patient (actual count is random 0–N).
        seed: Optional random seed for reproducibility.

    Returns:
        List of synthetic FHIR Condition dicts.

    Raises:
        ValueError: If *conditions* or *synthetic_patients* is empty.
        RuntimeError: If SDV is not installed.
    """
    if not SDV_AVAILABLE:
        raise RuntimeError(
            "SDV is not installed. Install with: pip install -r requirements-sdv.txt"
        )
    if not conditions:
        raise ValueError("conditions list must not be empty")
    if not synthetic_patients:
        raise ValueError("synthetic_patients list must not be empty")

    import random
    rng = random.Random(seed)

    rows = [_flatten_condition(c) for c in conditions]
    df = pd.DataFrame(rows)

    metadata = Metadata()
    metadata.add_table("conditions")
    for col in df.columns:
        metadata.update_column("conditions", col, sdtype="categorical")

    synthesizer = GaussianCopulaSynthesizer(
        metadata,
        enforce_min_max_values=False,
        enforce_rounding=False,
    )
    synthesizer.fit(df)

    result: list[dict] = []
    for patient in synthetic_patients:
        n = rng.randint(0, count_per_patient)
        if n > 0:
            syn_df = synthesizer.sample(num_rows=n)
            for row in syn_df.to_dict(orient="records"):
                result.append(_unflatten_condition(row, patient["id"], rng))

    return result
