"""Tests for analytics.synthetic — generate_synthetic_patients().

Runs locally without Docker.  Follow the same pattern as test_risk.py:
unittest.TestCase, helper functions, no fixtures, stdlib only.
"""
import re
import unittest

from analytics.synthetic import generate_synthetic_patients, _extract_distributions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(gender="male", birth_date="1980-06-15", postal="12345"):
    """Build a minimal FHIR Patient dict for testing."""
    r = {"resourceType": "Patient", "id": "p1"}
    if gender is not None:
        r["gender"] = gender
    if birth_date is not None:
        r["birthDate"] = birth_date
    if postal is not None:
        r["address"] = [{"postalCode": postal}]
    return r


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# TestExtractDistributions
# ---------------------------------------------------------------------------

class TestExtractDistributions(unittest.TestCase):

    def test_gender_extracted(self):
        patients = [_patient(gender="male"), _patient(gender="female")]
        genders, _, _ = _extract_distributions(patients)
        self.assertIn("male", genders)
        self.assertIn("female", genders)
        self.assertEqual(len(genders), 2)

    def test_birth_year_extracted(self):
        patients = [_patient(birth_date="1985-03-12"), _patient(birth_date="1972-11-01")]
        _, years, _ = _extract_distributions(patients)
        self.assertIn("1985", years)
        self.assertIn("1972", years)

    def test_zip_prefix_extracted(self):
        patients = [_patient(postal="12345"), _patient(postal="67890")]
        _, _, zips = _extract_distributions(patients)
        self.assertIn("123", zips)
        self.assertIn("678", zips)

    def test_missing_gender_uses_empty_string(self):
        patients = [_patient(gender=None)]
        genders, _, _ = _extract_distributions(patients)
        self.assertEqual(genders, [""])

    def test_missing_address_uses_empty_string(self):
        p = {"resourceType": "Patient", "id": "p1", "gender": "male", "birthDate": "1990-01-01"}
        _, _, zips = _extract_distributions([p])
        self.assertEqual(zips, [""])

    def test_short_birth_date_preserved(self):
        patients = [_patient(birth_date="198")]
        _, years, _ = _extract_distributions(patients)
        self.assertEqual(years, ["198"])

    def test_zip_truncated_to_three(self):
        patients = [_patient(postal="999999")]
        _, _, zips = _extract_distributions(patients)
        self.assertEqual(zips, ["999"])


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
        """Synthetic birth dates must be YYYY-01-01 (year-only, January)."""
        out = generate_synthetic_patients(self.patients, count=10, seed=1)
        date_re = re.compile(r"^\d{4}-01-01$")
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


if __name__ == "__main__":
    unittest.main()
