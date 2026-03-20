"""Synthetic FHIR Patient generator — statistical synthesis from de-identified data.

Generates synthetic FHIR Patient resources that preserve the statistical
distributions of the input dataset (gender, birth year, zip prefix) without
copying any real patient records.  No new dependencies — stdlib only.

Usage
-----
    from analytics.synthetic import generate_synthetic_patients
    patients = [...]          # list of de-identified FHIR Patient dicts
    synthetic = generate_synthetic_patients(patients, count=100, seed=42)

The output resources are tagged with the ``SYN`` observation value code so
downstream systems can distinguish synthetic from real de-identified data.
"""
from __future__ import annotations

import random
import uuid
from collections import Counter
from typing import Any


# ---------------------------------------------------------------------------
# Distribution extraction
# ---------------------------------------------------------------------------

def _extract_distributions(patients: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Return (genders, birth_years, zip_prefixes) sampled from *patients*.

    Each list has one entry per patient to allow weighted sampling via
    ``random.choice()``.  Empty / missing values are included as-is so that
    the synthetic output mirrors the completeness level of the input.
    """
    genders: list[str] = []
    birth_years: list[str] = []
    zip_prefixes: list[str] = []

    for p in patients:
        genders.append((p.get("gender") or "").strip().lower())

        birth_date = (p.get("birthDate") or "").strip()
        birth_years.append(birth_date[:4] if len(birth_date) >= 4 else birth_date)

        addresses = p.get("address") or []
        postal = ""
        if isinstance(addresses, list) and addresses:
            postal = str(addresses[0].get("postalCode") or "")
        zip_prefixes.append(postal[:3])

    return genders, birth_years, zip_prefixes


# ---------------------------------------------------------------------------
# Synthetic patient builder
# ---------------------------------------------------------------------------

def _make_patient(
    rng: random.Random,
    genders: list[str],
    birth_years: list[str],
    zip_prefixes: list[str],
) -> dict[str, Any]:
    """Build one synthetic FHIR Patient resource."""
    gender = rng.choice(genders)

    # Sample birth year and apply ±2-year jitter for diversity
    raw_year = rng.choice(birth_years)
    if raw_year.isdigit() and len(raw_year) == 4:
        jittered = rng.randint(int(raw_year) - 2, int(raw_year) + 2)
        birth_date = f"{jittered}-01-01"
    else:
        birth_date = raw_year  # preserve missing / non-standard values as-is

    zip_prefix = rng.choice(zip_prefixes)
    # Pad to a plausible postal code to keep FHIR structure valid
    postal_code = f"{zip_prefix}000" if zip_prefix else ""

    resource: dict[str, Any] = {
        "resourceType": "Patient",
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "meta": {
            "tag": [
                {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
                    "code": "SYN",
                    "display": "synthetic",
                }
            ]
        },
    }

    if gender:
        resource["gender"] = gender

    if birth_date:
        resource["birthDate"] = birth_date

    if postal_code:
        resource["address"] = [{"postalCode": postal_code}]

    return resource


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_synthetic_patients(
    patients: list[dict],
    count: int,
    seed: int | None = None,
) -> list[dict]:
    """Generate *count* synthetic FHIR Patient resources from *patients*.

    Statistical distributions for gender, birth year, and zip prefix are
    sampled from the input.  Each synthetic record receives a new UUID and
    is tagged with the ``SYN`` code so it is clearly distinguishable from
    real de-identified data.

    Args:
        patients: List of de-identified FHIR Patient dicts used as the
                  distribution source.  Must contain at least one record.
        count: Number of synthetic patients to generate (1 – 10 000).
        seed: Optional random seed for reproducibility.

    Returns:
        List of *count* synthetic FHIR Patient dicts.

    Raises:
        ValueError: If *patients* is empty or *count* is out of range.
    """
    if not patients:
        raise ValueError("patients list must not be empty — no distribution to sample from")
    if count < 1 or count > 10_000:
        raise ValueError(f"count must be between 1 and 10 000, got {count}")

    rng = random.Random(seed)
    genders, birth_years, zip_prefixes = _extract_distributions(patients)

    return [
        _make_patient(rng, genders, birth_years, zip_prefixes)
        for _ in range(count)
    ]
