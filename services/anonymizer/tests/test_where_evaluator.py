"""Unit tests for the native .where(url=...) FHIRPath evaluator.

Tests that _parse_where_plan, _evaluate_where_path, and _classify_match
handle all .where() expression forms used in production config profiles
with output identical to fhirpathpy.
"""

import unittest
from pipeline.rule_matcher import (
    _classify_match,
    _evaluate_where_path,
    _parse_where_plan,
    clear_rule_caches,
)


# ---------------------------------------------------------------------------
# Synthetic FHIR Patient with extensions matching production config profiles
# ---------------------------------------------------------------------------

_PATIENT = {
    "resourceType": "Patient",
    "id": "test-001",
    "extension": [
        {
            "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
            "extension": [
                {"url": "ombCategory", "valueCoding": {"code": "2106-3", "display": "White"}},
                {"url": "text", "valueString": "White"},
            ],
        },
        {
            "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity",
            "extension": [
                {"url": "ombCategory", "valueCoding": {"code": "2186-5", "display": "Not Hispanic"}},
                {"url": "text", "valueString": "Not Hispanic or Latino"},
            ],
        },
        {
            "url": "http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName",
            "valueString": "Smith",
        },
        {
            "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-birthsex",
            "valueCode": "M",
        },
        {
            "url": "http://hl7.org/fhir/StructureDefinition/patient-birthPlace",
            "valueAddress": {"city": "Springfield", "state": "IL", "country": "US"},
        },
        {
            "url": "http://synthetichealth.github.io/synthea/disability-adjusted-life-years",
            "valueDecimal": 65.2,
        },
        {
            "url": "http://synthetichealth.github.io/synthea/quality-adjusted-life-years",
            "valueDecimal": 70.5,
        },
    ],
}


class TestClassifyMatch(unittest.TestCase):
    """_classify_match returns 'where' for all .where(url=...) forms."""

    def test_simple_where_eq(self):
        expr = "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race')"
        self.assertEqual(_classify_match(expr), "where")

    def test_where_with_trailing_field(self):
        expr = "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName').valueString"
        self.assertEqual(_classify_match(expr), "where")

    def test_where_with_deep_trailing(self):
        expr = "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-birthPlace').valueAddress.city"
        self.assertEqual(_classify_match(expr), "where")

    def test_chained_where(self):
        expr = "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race').extension.where(url='ombCategory')"
        self.assertEqual(_classify_match(expr), "where")

    def test_chained_where_with_trailing(self):
        expr = "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race').extension.where(url='text').valueString"
        self.assertEqual(_classify_match(expr), "where")

    def test_starts_with(self):
        expr = "Patient.extension.where(url.startsWith('http://synthetichealth.github.io/synthea/'))"
        self.assertEqual(_classify_match(expr), "where")

    def test_simple_path_not_where(self):
        self.assertEqual(_classify_match("Patient.name"), "simple")

    def test_wildcard_not_where(self):
        self.assertEqual(_classify_match("*.meta.lastUpdated"), "wildcard")


class TestParseWherePlan(unittest.TestCase):
    """_parse_where_plan decomposes .where() expressions correctly."""

    def test_simple_eq(self):
        plan = _parse_where_plan(
            "Patient.extension.where(url='http://example.com/ext')"
        )
        self.assertIsNotNone(plan)
        rtype, segments, trailing = plan
        self.assertEqual(rtype, "Patient")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0], (("extension",), "http://example.com/ext", "eq"))
        self.assertEqual(trailing, ())

    def test_trailing_field(self):
        plan = _parse_where_plan(
            "Patient.extension.where(url='http://example.com').valueString"
        )
        self.assertIsNotNone(plan)
        _, _, trailing = plan
        self.assertEqual(trailing, ("valueString",))

    def test_deep_trailing(self):
        plan = _parse_where_plan(
            "Patient.extension.where(url='http://example.com').valueAddress.city"
        )
        self.assertIsNotNone(plan)
        _, _, trailing = plan
        self.assertEqual(trailing, ("valueAddress", "city"))

    def test_chained_where(self):
        plan = _parse_where_plan(
            "Patient.extension.where(url='http://outer').extension.where(url='inner')"
        )
        self.assertIsNotNone(plan)
        _, segments, trailing = plan
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0], (("extension",), "http://outer", "eq"))
        self.assertEqual(segments[1], (("extension",), "inner", "eq"))
        self.assertEqual(trailing, ())

    def test_starts_with(self):
        plan = _parse_where_plan(
            "Patient.extension.where(url.startsWith('http://prefix/'))"
        )
        self.assertIsNotNone(plan)
        _, segments, _ = plan
        self.assertEqual(segments[0][2], "startsWith")
        self.assertEqual(segments[0][1], "http://prefix/")

    def test_returns_none_for_simple(self):
        self.assertIsNone(_parse_where_plan("Patient.name"))

    def test_returns_none_for_wildcard(self):
        self.assertIsNone(_parse_where_plan("*.id"))

    def test_returns_none_for_empty(self):
        self.assertIsNone(_parse_where_plan(""))

    def test_returns_none_for_no_where(self):
        self.assertIsNone(_parse_where_plan("Patient.extension"))


class TestEvaluateWherePath(unittest.TestCase):
    """.where() evaluator produces correct results in fhirpathpy .log() format."""

    def setUp(self):
        clear_rule_caches()

    def test_simple_eq_returns_matching_extension(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race')",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"]["url"],
                         "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race")
        self.assertIn("path", results[0])

    def test_trailing_value_string(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName').valueString",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "Smith")

    def test_trailing_deep_field(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-birthPlace').valueAddress.city",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "Springfield")

    def test_chained_where(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race').extension.where(url='ombCategory')",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"]["valueCoding"]["code"], "2106-3")

    def test_chained_where_with_trailing(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race').extension.where(url='text').valueString",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "White")

    def test_starts_with_matches_multiple(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url.startsWith('http://synthetichealth.github.io/synthea/'))",
        )
        self.assertEqual(len(results), 2)
        urls = {r["value"]["url"] for r in results}
        self.assertIn("http://synthetichealth.github.io/synthea/disability-adjusted-life-years", urls)
        self.assertIn("http://synthetichealth.github.io/synthea/quality-adjusted-life-years", urls)

    def test_non_matching_url_returns_empty(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://no-such-extension.com')",
        )
        self.assertEqual(results, [])

    def test_wrong_resource_type_returns_empty(self):
        obs = {"resourceType": "Observation", "id": "obs-1"}
        results = _evaluate_where_path(
            obs,
            "Patient.extension.where(url='http://example.com')",
        )
        self.assertEqual(results, [])

    def test_missing_extension_array_returns_empty(self):
        pat = {"resourceType": "Patient", "id": "no-ext"}
        results = _evaluate_where_path(
            pat,
            "Patient.extension.where(url='http://example.com')",
        )
        self.assertEqual(results, [])

    def test_non_dict_resource_returns_empty(self):
        results = _evaluate_where_path(
            "not a dict",
            "Patient.extension.where(url='http://example.com')",
        )
        self.assertEqual(results, [])

    def test_output_format_has_path_and_value(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-birthsex')",
        )
        self.assertEqual(len(results), 1)
        self.assertIn("path", results[0])
        self.assertIn("value", results[0])
        self.assertIsInstance(results[0]["path"], str)

    def test_ethnicity_nested(self):
        """Ethnicity ombCategory via chained .where()."""
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-ethnicity').extension.where(url='text').valueString",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "Not Hispanic or Latino")

    def test_birth_place_state(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-birthPlace').valueAddress.state",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "IL")

    def test_birth_place_country(self):
        results = _evaluate_where_path(
            _PATIENT,
            "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-birthPlace').valueAddress.country",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["value"], "US")


if __name__ == "__main__":
    unittest.main()
