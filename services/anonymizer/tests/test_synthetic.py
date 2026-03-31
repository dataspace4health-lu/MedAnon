"""Tests for analytics.synthetic — synthetic FHIR resource generation.

Runs locally without Docker.  Follow the same pattern as test_risk.py:
unittest.TestCase, helper functions, no fixtures, stdlib only.
"""
import re
import unittest

from analytics.synthetic import (
    generate_synthetic_patients,
    generate_synthetic_conditions,
    _extract_distributions,
    _extract_condition_distributions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(gender="male", birth_date="1980-06-15", postal="12345",
             marital_code=None, language_code=None):
    """Build a minimal FHIR Patient dict for testing."""
    r = {"resourceType": "Patient", "id": "p1"}
    if gender is not None:
        r["gender"] = gender
    if birth_date is not None:
        r["birthDate"] = birth_date
    if postal is not None:
        r["address"] = [{"postalCode": postal}]
    if marital_code is not None:
        r["maritalStatus"] = {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus", "code": marital_code}],
        }
    if language_code is not None:
        r["communication"] = [{
            "language": {"coding": [{"system": "urn:ietf:bcp:47", "code": language_code}]},
        }]
    return r


def _condition(code_text="Diabetes", clinical_status="active", patient_id="p1"):
    """Build a minimal FHIR Condition dict for testing."""
    return {
        "resourceType": "Condition",
        "id": "c1",
        "subject": {"reference": f"Patient/{patient_id}"},
        "code": {"text": code_text},
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": clinical_status}],
        },
        "category": [{
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "encounter-diagnosis"}],
        }],
    }


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# TestExtractDistributions
# ---------------------------------------------------------------------------

class TestExtractDistributions(unittest.TestCase):

    def test_gender_extracted(self):
        patients = [_patient(gender="male"), _patient(gender="female")]
        dists = _extract_distributions(patients)
        self.assertIn("male", dists["genders"])
        self.assertIn("female", dists["genders"])
        self.assertEqual(len(dists["genders"]), 2)

    def test_birth_year_extracted(self):
        patients = [_patient(birth_date="1985-03-12"), _patient(birth_date="1972-11-01")]
        dists = _extract_distributions(patients)
        self.assertIn("1985", dists["birth_years"])
        self.assertIn("1972", dists["birth_years"])

    def test_zip_prefix_extracted(self):
        patients = [_patient(postal="12345"), _patient(postal="67890")]
        dists = _extract_distributions(patients)
        self.assertIn("123", dists["zip_prefixes"])
        self.assertIn("678", dists["zip_prefixes"])

    def test_missing_gender_uses_empty_string(self):
        patients = [_patient(gender=None)]
        dists = _extract_distributions(patients)
        self.assertEqual(dists["genders"], [""])

    def test_missing_address_uses_empty_string(self):
        p = {"resourceType": "Patient", "id": "p1", "gender": "male", "birthDate": "1990-01-01"}
        dists = _extract_distributions([p])
        self.assertEqual(dists["zip_prefixes"], [""])

    def test_short_birth_date_preserved(self):
        patients = [_patient(birth_date="198")]
        dists = _extract_distributions(patients)
        self.assertEqual(dists["birth_years"], ["198"])

    def test_zip_truncated_to_three(self):
        patients = [_patient(postal="999999")]
        dists = _extract_distributions(patients)
        self.assertEqual(dists["zip_prefixes"], ["999"])

    def test_marital_status_extracted(self):
        patients = [_patient(marital_code="M"), _patient(marital_code="S")]
        dists = _extract_distributions(patients)
        self.assertIn("M", dists["marital_statuses"])
        self.assertIn("S", dists["marital_statuses"])

    def test_missing_marital_status_uses_empty_string(self):
        patients = [_patient()]
        dists = _extract_distributions(patients)
        self.assertEqual(dists["marital_statuses"], [""])

    def test_language_extracted(self):
        patients = [_patient(language_code="en"), _patient(language_code="de")]
        dists = _extract_distributions(patients)
        self.assertIn("en", dists["languages"])
        self.assertIn("de", dists["languages"])

    def test_missing_language_uses_empty_string(self):
        patients = [_patient()]
        dists = _extract_distributions(patients)
        self.assertEqual(dists["languages"], [""])


# ---------------------------------------------------------------------------
# TestOutputStructure  (via generate_synthetic_patients)
# ---------------------------------------------------------------------------

class TestOutputStructure(unittest.TestCase):

    def setUp(self):
        self.patients = [
            _patient("male", "1970-01-01", "10000"),
            _patient("female", "1985-06-15", "20200"),
            _patient("other", "1992-12-31", "30300"),
        ]

    def test_resource_type_is_patient(self):
        out = generate_synthetic_patients(self.patients, count=5, seed=1)
        for r in out:
            self.assertEqual(r["resourceType"], "Patient")

    def test_id_is_uuid(self):
        out = generate_synthetic_patients(self.patients, count=5, seed=1)
        for r in out:
            self.assertRegex(r["id"], _UUID_RE)

    def test_syn_tag_present(self):
        out = generate_synthetic_patients(self.patients, count=1, seed=1)
        tags = out[0]["meta"]["tag"]
        codes = [t["code"] for t in tags]
        self.assertIn("SYN", codes)

    def test_syn_tag_system(self):
        out = generate_synthetic_patients(self.patients, count=1, seed=1)
        tag = out[0]["meta"]["tag"][0]
        self.assertEqual(
            tag["system"],
            "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
        )

    def test_gender_in_known_set(self):
        out = generate_synthetic_patients(self.patients, count=20, seed=42)
        valid = {"male", "female", "other", ""}
        for r in out:
            if "gender" in r:
                self.assertIn(r["gender"], valid)

    def test_birth_date_format(self):
        """Synthetic birth dates must be YYYY-MM-DD with jittered year."""
        out = generate_synthetic_patients(self.patients, count=10, seed=1)
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        for r in out:
            if "birthDate" in r:
                self.assertRegex(r["birthDate"], date_re)


# ---------------------------------------------------------------------------
# TestGenerateSyntheticPatients
# ---------------------------------------------------------------------------

class TestGenerateSyntheticPatients(unittest.TestCase):

    def _many(self):
        """Return a realistic list with diverse distributions."""
        return [
            _patient("male",   "1960-01-01", "10111"),
            _patient("female", "1975-06-15", "20222"),
            _patient("male",   "1982-03-20", "10111"),
            _patient("female", "1990-09-01", "30333"),
            _patient("other",  "2000-12-31", "40444"),
        ]

    def test_count_respected(self):
        out = generate_synthetic_patients(self._many(), count=50, seed=7)
        self.assertEqual(len(out), 50)

    def test_count_one(self):
        out = generate_synthetic_patients(self._many(), count=1, seed=1)
        self.assertEqual(len(out), 1)

    def test_seed_reproducible(self):
        a = generate_synthetic_patients(self._many(), count=10, seed=99)
        b = generate_synthetic_patients(self._many(), count=10, seed=99)
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])

    def test_different_seeds_produce_different_ids(self):
        a = generate_synthetic_patients(self._many(), count=10, seed=1)
        b = generate_synthetic_patients(self._many(), count=10, seed=2)
        self.assertNotEqual([r["id"] for r in a], [r["id"] for r in b])

    def test_no_real_ids_copied(self):
        patients = self._many()
        real_ids = {p["id"] for p in patients}
        out = generate_synthetic_patients(patients, count=30, seed=5)
        synthetic_ids = {r["id"] for r in out}
        self.assertEqual(len(real_ids & synthetic_ids), 0)

    def test_raises_on_empty_patients(self):
        with self.assertRaises(ValueError):
            generate_synthetic_patients([], count=10)

    def test_raises_on_count_zero(self):
        with self.assertRaises(ValueError):
            generate_synthetic_patients(self._many(), count=0)

    def test_raises_on_count_negative(self):
        with self.assertRaises(ValueError):
            generate_synthetic_patients(self._many(), count=-1)

    def test_raises_on_count_over_limit(self):
        with self.assertRaises(ValueError):
            generate_synthetic_patients(self._many(), count=10_001)

    def test_no_seed_still_works(self):
        out = generate_synthetic_patients(self._many(), count=5)
        self.assertEqual(len(out), 5)

    def test_single_input_patient(self):
        """A single input patient should still produce valid output."""
        out = generate_synthetic_patients([_patient()], count=3, seed=1)
        self.assertEqual(len(out), 3)
        for r in out:
            self.assertEqual(r["resourceType"], "Patient")

    def test_marital_status_in_output(self):
        patients = [_patient(marital_code="M"), _patient(marital_code="S")]
        out = generate_synthetic_patients(patients, count=20, seed=42)
        codes = {r["maritalStatus"]["coding"][0]["code"] for r in out if "maritalStatus" in r}
        self.assertTrue(codes.issubset({"M", "S"}))

    def test_language_in_output(self):
        patients = [_patient(language_code="en"), _patient(language_code="de")]
        out = generate_synthetic_patients(patients, count=20, seed=42)
        codes = {r["communication"][0]["language"]["coding"][0]["code"] for r in out if "communication" in r}
        self.assertTrue(codes.issubset({"en", "de"}))


# ---------------------------------------------------------------------------
# TestExtractConditionDistributions
# ---------------------------------------------------------------------------

class TestExtractConditionDistributions(unittest.TestCase):

    def test_code_extracted(self):
        conditions = [_condition(code_text="Diabetes"), _condition(code_text="Hypertension")]
        dists = _extract_condition_distributions(conditions)
        texts = [c.get("text") for c in dists["codes"]]
        self.assertIn("Diabetes", texts)
        self.assertIn("Hypertension", texts)

    def test_clinical_status_extracted(self):
        conditions = [_condition(clinical_status="active"), _condition(clinical_status="resolved")]
        dists = _extract_condition_distributions(conditions)
        self.assertIn("active", dists["clinical_statuses"])
        self.assertIn("resolved", dists["clinical_statuses"])

    def test_missing_code_defaults_to_unknown(self):
        c = {"resourceType": "Condition", "id": "c1"}
        dists = _extract_condition_distributions([c])
        self.assertEqual(dists["codes"][0], {"text": "Unknown"})

    def test_missing_clinical_status_defaults_to_active(self):
        c = {"resourceType": "Condition", "id": "c1"}
        dists = _extract_condition_distributions([c])
        self.assertEqual(dists["clinical_statuses"][0], "active")


# ---------------------------------------------------------------------------
# TestGenerateSyntheticConditions
# ---------------------------------------------------------------------------

class TestGenerateSyntheticConditions(unittest.TestCase):

    def setUp(self):
        self.patients = generate_synthetic_patients(
            [_patient("male", "1970-01-01", "10000"), _patient("female", "1985-06-15", "20200")],
            count=5, seed=1,
        )
        self.conditions = [
            _condition("Diabetes", "active"),
            _condition("Hypertension", "resolved"),
        ]

    def test_resource_type_is_condition(self):
        out = generate_synthetic_conditions(self.conditions, self.patients, count_per_patient=2, seed=42)
        for r in out:
            self.assertEqual(r["resourceType"], "Condition")

    def test_subject_references_synthetic_patients(self):
        out = generate_synthetic_conditions(self.conditions, self.patients, count_per_patient=2, seed=42)
        valid_refs = {f"Patient/{p['id']}" for p in self.patients}
        for r in out:
            self.assertIn(r["subject"]["reference"], valid_refs)

    def test_syn_tag_present(self):
        out = generate_synthetic_conditions(self.conditions, self.patients, count_per_patient=1, seed=42)
        if out:
            codes = [t["code"] for t in out[0]["meta"]["tag"]]
            self.assertIn("SYN", codes)

    def test_id_is_uuid(self):
        out = generate_synthetic_conditions(self.conditions, self.patients, count_per_patient=2, seed=42)
        for r in out:
            self.assertRegex(r["id"], _UUID_RE)

    def test_seed_reproducible(self):
        a = generate_synthetic_conditions(self.conditions, self.patients, count_per_patient=2, seed=99)
        b = generate_synthetic_conditions(self.conditions, self.patients, count_per_patient=2, seed=99)
        self.assertEqual([r["id"] for r in a], [r["id"] for r in b])

    def test_raises_on_empty_conditions(self):
        with self.assertRaises(ValueError):
            generate_synthetic_conditions([], self.patients)

    def test_raises_on_empty_patients(self):
        with self.assertRaises(ValueError):
            generate_synthetic_conditions(self.conditions, [])


if __name__ == "__main__":
    unittest.main()
