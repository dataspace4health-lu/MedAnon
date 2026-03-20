"""Unit tests for analytics.risk — runs without Docker."""
import json
import sys
import unittest
from pathlib import Path

# Allow running from repo root or services/anonymizer/
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from analytics.risk import (
    assess_risk,
    build_conditions_map,
    compute_k_anonymity,
    compute_l_diversity,
    extract_quasi_identifiers,
)


def _patient(id_="p1", gender="male", birth_date="1980-05-10", postal="12345"):
    r = {"resourceType": "Patient", "id": id_}
    if gender is not None:
        r["gender"] = gender
    if birth_date is not None:
        r["birthDate"] = birth_date
    if postal is not None:
        r["address"] = [{"postalCode": postal}]
    return r


def _condition(patient_id, code):
    return {
        "resourceType": "Condition",
        "id": f"cond-{patient_id}-{code}",
        "subject": {"reference": f"Patient/{patient_id}"},
        "code": {"coding": [{"code": code, "system": "http://snomed.info/sct"}]},
    }


def _ndjson(*resources):
    return "\n".join(json.dumps(r) for r in resources)


class TestExtractQuasiIdentifiers(unittest.TestCase):
    def test_full_fields(self):
        patients = [_patient(gender="female", birth_date="1975-03-15", postal="75001")]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi, [("female", "1975", "750")])

    def test_year_only_birth_date(self):
        patients = [_patient(birth_date="1985")]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi[0][1], "1985")

    def test_missing_gender(self):
        patients = [_patient(gender=None)]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi[0][0], "")

    def test_missing_birth_date(self):
        patients = [_patient(birth_date=None)]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi[0][1], "")

    def test_missing_address(self):
        patients = [_patient(postal=None)]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi[0][2], "")

    def test_short_postal(self):
        patients = [_patient(postal="12")]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi[0][2], "12")

    def test_gender_normalised_lowercase(self):
        patients = [_patient(gender="MALE")]
        qi = extract_quasi_identifiers(patients)
        self.assertEqual(qi[0][0], "male")


class TestBuildConditionsMap(unittest.TestCase):
    def test_basic(self):
        resources = [
            _condition("p1", "73211009"),
            _condition("p1", "44054006"),
            _condition("p2", "73211009"),
        ]
        m = build_conditions_map(resources)
        self.assertEqual(m["p1"], {"73211009", "44054006"})
        self.assertEqual(m["p2"], {"73211009"})

    def test_non_condition_skipped(self):
        resources = [_patient(), _condition("p1", "73211009")]
        m = build_conditions_map(resources)
        self.assertIn("p1", m)
        self.assertNotIn("p1_patient", m)

    def test_no_conditions(self):
        m = build_conditions_map([_patient()])
        self.assertEqual(m, {})

    def test_patient_slash_reference(self):
        r = _condition("abc", "123")
        m = build_conditions_map([r])
        self.assertIn("abc", m)


class TestComputeKAnonymity(unittest.TestCase):
    def test_empty(self):
        result = compute_k_anonymity([])
        s = result["summary"]
        self.assertEqual(s["total_records"], 0)
        self.assertEqual(s["risk_level"], "low")
        self.assertEqual(result["groups"], [])

    def test_single_record_critical(self):
        qi = [("male", "1980", "123")]
        result = compute_k_anonymity(qi)
        s = result["summary"]
        self.assertEqual(s["min_k"], 1)
        self.assertEqual(s["risk_level"], "critical")
        self.assertAlmostEqual(s["prosecutor_risk"], 1.0)
        self.assertEqual(s["singleton_groups"], 1)

    def test_two_identical_records_high(self):
        qi = [("male", "1980", "123"), ("male", "1980", "123")]
        result = compute_k_anonymity(qi)
        s = result["summary"]
        self.assertEqual(s["min_k"], 2)
        self.assertEqual(s["risk_level"], "high")
        self.assertAlmostEqual(s["prosecutor_risk"], 0.5)

    def test_medium_risk(self):
        qi = [("male", "1980", "123")] * 3 + [("female", "1990", "456")] * 3
        result = compute_k_anonymity(qi)
        s = result["summary"]
        self.assertEqual(s["min_k"], 3)
        self.assertEqual(s["risk_level"], "medium")

    def test_low_risk(self):
        qi = [("male", "1980", "123")] * 5
        result = compute_k_anonymity(qi)
        self.assertEqual(result["summary"]["risk_level"], "low")

    def test_marketer_risk_formula(self):
        # 2 groups of 3 → marketer = 2/6 = 0.333...
        qi = [("m", "1980", "123")] * 3 + [("f", "1990", "456")] * 3
        result = compute_k_anonymity(qi)
        self.assertAlmostEqual(result["summary"]["marketer_risk"], 2 / 6, places=4)

    def test_groups_sorted_ascending_k(self):
        qi = [("m", "1980", "123")] * 5 + [("f", "1990", "456")] * 1
        result = compute_k_anonymity(qi)
        sizes = [g["k"] for g in result["groups"]]
        self.assertEqual(sizes, sorted(sizes))

    def test_records_with_missing_qi(self):
        qi = [("", "1980", "123"), ("male", "1980", "456")]
        result = compute_k_anonymity(qi)
        self.assertEqual(result["summary"]["records_with_missing_qi"], 1)


class TestComputeLDiversity(unittest.TestCase):
    def test_no_conditions(self):
        qi = [("male", "1980", "123")]
        result = compute_l_diversity(qi, ["p1"], {})
        self.assertFalse(result["computed"])

    def test_l_diversity_computed(self):
        qi = [("male", "1980", "123"), ("male", "1980", "123")]
        patient_ids = ["p1", "p2"]
        conditions = {"p1": {"73211009"}, "p2": {"44054006"}}
        result = compute_l_diversity(qi, patient_ids, conditions)
        self.assertTrue(result["computed"])
        self.assertEqual(result["min_l"], 2)
        self.assertEqual(result["violations"], 0)

    def test_l_diversity_violation(self):
        qi = [("male", "1980", "123"), ("male", "1980", "123")]
        patient_ids = ["p1", "p2"]
        conditions = {"p1": {"73211009"}, "p2": {"73211009"}}  # same code
        result = compute_l_diversity(qi, patient_ids, conditions)
        self.assertTrue(result["computed"])
        self.assertEqual(result["min_l"], 1)
        self.assertEqual(result["violations"], 1)


class TestAssessRisk(unittest.TestCase):
    def test_empty_string(self):
        result = assess_risk("")
        s = result["summary"]
        self.assertEqual(s["total_records"], 0)
        self.assertIn("No Patient resources", result["warnings"][0])

    def test_no_patients(self):
        ndjson = _ndjson(_condition("p1", "123"))
        result = assess_risk(ndjson)
        self.assertEqual(result["summary"]["total_records"], 0)

    def test_single_patient_critical(self):
        ndjson = _ndjson(_patient("p1"))
        result = assess_risk(ndjson)
        self.assertEqual(result["summary"]["risk_level"], "critical")
        self.assertEqual(result["summary"]["total_records"], 1)

    def test_mixed_ndjson_patients_and_conditions(self):
        resources = [
            _patient("p1", "male", "1980-01-01", "12345"),
            _patient("p2", "male", "1980-01-01", "12345"),
            _patient("p3", "male", "1980-01-01", "12345"),
            _condition("p1", "73211009"),
            _condition("p2", "44054006"),
            _condition("p3", "195662009"),
        ]
        result = assess_risk(_ndjson(*resources))
        s = result["summary"]
        self.assertEqual(s["total_records"], 3)
        self.assertEqual(s["min_k"], 3)
        self.assertEqual(result["meta"]["condition_lines"], 3)
        self.assertTrue(result["l_diversity"]["computed"])

    def test_non_patient_resources_skipped(self):
        resources = [
            _patient("p1"),
            {"resourceType": "Observation", "id": "obs1"},
        ]
        result = assess_risk(_ndjson(*resources))
        self.assertEqual(result["summary"]["total_records"], 1)
        self.assertEqual(result["meta"]["input_lines"], 2)

    def test_blank_lines_and_comments_skipped(self):
        ndjson = "\n".join([
            "// comment",
            "",
            json.dumps(_patient("p1")),
            "",
        ])
        result = assess_risk(ndjson)
        self.assertEqual(result["summary"]["total_records"], 1)

    def test_invalid_json_raises(self):
        with self.assertRaises(ValueError) as ctx:
            assess_risk("not json\n")
        self.assertIn("line 1", str(ctx.exception))

    def test_missing_qi_warning(self):
        ndjson = _ndjson(_patient("p1", gender=None))
        result = assess_risk(ndjson)
        self.assertTrue(any("missing" in w for w in result["warnings"]))

    def test_meta_fields_present(self):
        result = assess_risk(_ndjson(_patient("p1"), _patient("p2")))
        meta = result["meta"]
        self.assertIn("computed_at", meta)
        self.assertIn("patient_lines", meta)
        self.assertIn("condition_lines", meta)


if __name__ == "__main__":
    unittest.main()
