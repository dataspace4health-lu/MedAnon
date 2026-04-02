"""Tests for analytics.synthetic_sdv — SDV-powered synthetic FHIR generation.

These tests are skipped when the ``sdv`` package is not installed.
"""
import re
import unittest

try:
    from medanon_core.analytics.synthetic_sdv import (
        SDV_AVAILABLE,
        _flatten_patient,
        _flatten_condition,
        _unflatten_patient,
        _unflatten_condition,
        generate_synthetic_patients_sdv,
        generate_synthetic_conditions_sdv,
    )
except ImportError:
    SDV_AVAILABLE = False


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(gender="male", birth_date="1980-06-15", postal="12345",
             marital_code=None, language_code=None):
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


def _condition(code_text="Diabetes", clinical_status="active"):
    return {
        "resourceType": "Condition",
        "id": "c1",
        "code": {"text": code_text},
        "clinicalStatus": {
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical", "code": clinical_status}],
        },
        "category": [{
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-category", "code": "encounter-diagnosis"}],
        }],
    }


# ---------------------------------------------------------------------------
# Flatten/unflatten tests — these run even without SDV installed, because
# the functions only depend on stdlib.  We guard with SDV_AVAILABLE anyway
# since the module import might fail entirely.
# ---------------------------------------------------------------------------

@unittest.skipUnless(SDV_AVAILABLE, "sdv package not installed")
class TestFlattenPatient(unittest.TestCase):

    def test_all_fields_extracted(self):
        p = _patient("female", "1985-03-12", "20200", marital_code="M", language_code="en")
        row = _flatten_patient(p)
        self.assertEqual(row["gender"], "female")
        self.assertEqual(row["birth_year"], "1985")
        self.assertEqual(row["zip_prefix"], "202")
        self.assertEqual(row["marital_status"], "M")
        self.assertEqual(row["language"], "en")

    def test_missing_fields_use_empty_string(self):
        p = {"resourceType": "Patient", "id": "p1"}
        row = _flatten_patient(p)
        self.assertEqual(row["gender"], "")
        self.assertEqual(row["birth_year"], "")
        self.assertEqual(row["zip_prefix"], "")
        self.assertEqual(row["marital_status"], "")
        self.assertEqual(row["language"], "")


@unittest.skipUnless(SDV_AVAILABLE, "sdv package not installed")
class TestFlattenCondition(unittest.TestCase):

    def test_basic_condition(self):
        c = _condition("Diabetes", "active")
        row = _flatten_condition(c)
        self.assertEqual(row["diagnosis_code"], "Diabetes")
        self.assertEqual(row["clinical_status"], "active")
        self.assertEqual(row["category"], "encounter-diagnosis")

    def test_missing_fields(self):
        c = {"resourceType": "Condition", "id": "c1"}
        row = _flatten_condition(c)
        self.assertEqual(row["diagnosis_code"], "Unknown")
        self.assertEqual(row["clinical_status"], "active")
        self.assertEqual(row["category"], "encounter-diagnosis")


@unittest.skipUnless(SDV_AVAILABLE, "sdv package not installed")
class TestUnflattenPatient(unittest.TestCase):

    def test_full_row(self):
        import random
        rng = random.Random(42)
        row = {"gender": "male", "birth_year": "1985", "zip_prefix": "123",
               "marital_status": "M", "language": "en"}
        patient = _unflatten_patient(row, rng)
        self.assertEqual(patient["resourceType"], "Patient")
        self.assertRegex(patient["id"], _UUID_RE)
        self.assertEqual(patient["gender"], "male")
        self.assertIn("birthDate", patient)
        self.assertEqual(patient["address"][0]["postalCode"], "123000")
        self.assertEqual(patient["maritalStatus"]["coding"][0]["code"], "M")
        self.assertEqual(patient["communication"][0]["language"]["coding"][0]["code"], "en")
        self.assertEqual(patient["meta"]["tag"][0]["code"], "SYN")

    def test_empty_fields_omitted(self):
        import random
        rng = random.Random(42)
        row = {"gender": "", "birth_year": "", "zip_prefix": "",
               "marital_status": "", "language": ""}
        patient = _unflatten_patient(row, rng)
        self.assertNotIn("gender", patient)
        self.assertNotIn("birthDate", patient)
        self.assertNotIn("address", patient)
        self.assertNotIn("maritalStatus", patient)
        self.assertNotIn("communication", patient)


# ---------------------------------------------------------------------------
# End-to-end generation tests (require SDV)
# ---------------------------------------------------------------------------

@unittest.skipUnless(SDV_AVAILABLE, "sdv package not installed")
class TestGenerateSyntheticPatientsSdv(unittest.TestCase):

    def _many(self):
        return [
            _patient("male", "1960-01-01", "10111", "M", "en"),
            _patient("female", "1975-06-15", "20222", "S", "de"),
            _patient("male", "1982-03-20", "10111", "M", "en"),
            _patient("female", "1990-09-01", "30333", "D", "fr"),
            _patient("other", "2000-12-31", "40444", "S", "en"),
        ]

    def test_count_respected(self):
        out = generate_synthetic_patients_sdv(self._many(), count=20, seed=42)
        self.assertEqual(len(out), 20)

    def test_resource_type_is_patient(self):
        out = generate_synthetic_patients_sdv(self._many(), count=5, seed=1)
        for r in out:
            self.assertEqual(r["resourceType"], "Patient")

    def test_syn_tag_present(self):
        out = generate_synthetic_patients_sdv(self._many(), count=1, seed=1)
        codes = [t["code"] for t in out[0]["meta"]["tag"]]
        self.assertIn("SYN", codes)

    def test_id_is_uuid(self):
        out = generate_synthetic_patients_sdv(self._many(), count=5, seed=1)
        for r in out:
            self.assertRegex(r["id"], _UUID_RE)

    def test_raises_on_empty_patients(self):
        with self.assertRaises(ValueError):
            generate_synthetic_patients_sdv([], count=10)

    def test_raises_on_count_out_of_range(self):
        with self.assertRaises(ValueError):
            generate_synthetic_patients_sdv(self._many(), count=0)
        with self.assertRaises(ValueError):
            generate_synthetic_patients_sdv(self._many(), count=10_001)


@unittest.skipUnless(SDV_AVAILABLE, "sdv package not installed")
class TestGenerateSyntheticConditionsSdv(unittest.TestCase):

    def setUp(self):
        self.patients = generate_synthetic_patients_sdv(
            [_patient("male", "1970-01-01", "10000"), _patient("female", "1985-06-15", "20200")],
            count=5, seed=1,
        )
        self.conditions = [
            _condition("Diabetes", "active"),
            _condition("Hypertension", "resolved"),
        ]

    def test_resource_type_is_condition(self):
        out = generate_synthetic_conditions_sdv(self.conditions, self.patients, count_per_patient=2, seed=42)
        for r in out:
            self.assertEqual(r["resourceType"], "Condition")

    def test_subject_references_synthetic_patients(self):
        out = generate_synthetic_conditions_sdv(self.conditions, self.patients, count_per_patient=2, seed=42)
        valid_refs = {f"Patient/{p['id']}" for p in self.patients}
        for r in out:
            self.assertIn(r["subject"]["reference"], valid_refs)

    def test_raises_on_empty_conditions(self):
        with self.assertRaises(ValueError):
            generate_synthetic_conditions_sdv([], self.patients)

    def test_raises_on_empty_patients(self):
        with self.assertRaises(ValueError):
            generate_synthetic_conditions_sdv(self.conditions, [])


# ---------------------------------------------------------------------------
# Test that SDV_AVAILABLE flag works when SDV is not installed
# ---------------------------------------------------------------------------

class TestSdvAvailableFlag(unittest.TestCase):
    """Verifies the SDV_AVAILABLE flag is a bool (True or False)."""

    def test_flag_is_bool(self):
        self.assertIsInstance(SDV_AVAILABLE, bool)


if __name__ == "__main__":
    unittest.main()
