"""Tests for pipeline.action_dispatcher.dispatch_pass1."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import unittest
from unittest.mock import patch, MagicMock, call

from pipeline.action_dispatcher import dispatch_pass1, BatchWork


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MOD = "pipeline.action_dispatcher"


def _make_rule(name, match, action, params=None):
    """Build a minimal rule dict."""
    r = {"name": name, "match": match, "action": action}
    if params:
        r["params"] = params
    return r


def _make_element(path, value):
    """Build a minimal FHIRPath match element."""
    return {"path": path, "value": value}


class TestDispatchPass1SimpleRule(unittest.TestCase):
    """A single deident rule dispatches perform_deidentification."""

    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_simple_dispatch(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident,
    ):
        resource = {"resourceType": "Patient", "name": [{"family": "Doe"}]}
        rule = _make_rule("redact-name", "Patient.name", "redact")

        element = _make_element("Patient.name", [{"family": "Doe"}])
        mock_candidates.return_value = ["Patient.name"]
        mock_eval_simple.return_value = [element]

        result = dispatch_pass1(resource, [rule], {}, [], "skip")

        mock_candidates.assert_called_once_with("Patient.name", resource)
        mock_classify.assert_called_once_with("Patient.name")
        mock_eval_simple.assert_called_once_with(resource, "Patient.name")
        mock_deident.assert_called_once_with("redact", resource, element, {})
        self.assertEqual(result, [])


class TestDispatchPass1GpasWorkCollection(unittest.TestCase):
    """Action gpas_pseudonymize collects BatchWork instead of calling perform_*."""

    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}.perform_pseudonymization")
    @patch(f"{_MOD}._resolve_rule_params", return_value={"domain": "test"})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_gpas_returns_batch_work(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_pseudo, mock_deident,
    ):
        resource = {"resourceType": "Patient", "id": "123"}
        rule = _make_rule("pseudo-id", "Patient.id", "gpas_pseudonymize")

        element = _make_element("Patient.id", "123")
        mock_candidates.return_value = ["Patient.id"]
        mock_eval_simple.return_value = [element]

        result = dispatch_pass1(resource, [rule], {}, [], "skip")

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], BatchWork)
        self.assertEqual(result[0].rule, rule)
        self.assertEqual(result[0].element, element)
        self.assertEqual(result[0].serialized_value, "123")
        self.assertEqual(result[0].params, {"domain": "test"})

        mock_deident.assert_not_called()
        mock_pseudo.assert_not_called()


class TestDispatchPass1DuplicatePathFiltering(unittest.TestCase):
    """Two rules with the same action on the same path -- second is skipped."""

    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_duplicate_path_same_action_skipped(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident,
    ):
        resource = {"resourceType": "Patient", "name": [{"family": "Doe"}]}
        rule1 = _make_rule("redact-name-1", "Patient.name", "redact")
        rule2 = _make_rule("redact-name-2", "Patient.name", "redact")

        element = _make_element("Patient.name", [{"family": "Doe"}])
        mock_candidates.return_value = ["Patient.name"]
        mock_eval_simple.return_value = [element]

        result = dispatch_pass1(resource, [rule1, rule2], {}, [], "skip")

        # Only one call -- the first rule fires, the second is deduplicated
        mock_deident.assert_called_once_with("redact", resource, element, {})
        self.assertEqual(result, [])


class TestDispatchPass1DualPassDifferentActions(unittest.TestCase):
    """Two different deident actions on the same path -- both fire (defense-in-depth)."""

    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_scrub_text_and_nlp_scrub_both_fire(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident,
    ):
        resource = {"resourceType": "Patient", "text": {"div": "<div>John Doe born 1990</div>"}}
        rule_scrub = _make_rule("scrub-text", "Patient.text", "scrub_text")
        rule_nlp = _make_rule("nlp-scrub-text", "Patient.text", "nlp_scrub")

        element = _make_element("Patient.text", {"div": "<div>John Doe born 1990</div>"})
        mock_candidates.return_value = ["Patient.text"]
        mock_eval_simple.return_value = [element]

        result = dispatch_pass1(
            resource, [rule_scrub, rule_nlp], {}, [], "skip",
        )

        # Both actions fire because dedup is keyed on (path, action) not (path, category)
        self.assertEqual(mock_deident.call_count, 2)
        first_call, second_call = mock_deident.call_args_list
        self.assertEqual(first_call, call("scrub_text", resource, element, {}))
        self.assertEqual(second_call, call("nlp_scrub", resource, element, {}))
        self.assertEqual(result, [])


class TestDispatchPass1DifferentCategoriesSamePath(unittest.TestCase):
    """Deident + pseudo rules on the same path -- both fire (different categories)."""

    @patch(f"{_MOD}.perform_pseudonymization")
    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_different_categories_both_fire(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident, mock_pseudo,
    ):
        resource = {"resourceType": "Patient", "id": "123"}
        rule_deident = _make_rule("redact-id", "Patient.id", "redact")
        rule_pseudo = _make_rule("encrypt-id", "Patient.id", "encrypt")

        element = _make_element("Patient.id", "123")
        mock_candidates.return_value = ["Patient.id"]
        mock_eval_simple.return_value = [element]

        result = dispatch_pass1(
            resource, [rule_deident, rule_pseudo], {}, [], "skip",
        )

        mock_deident.assert_called_once_with("redact", resource, element, {})
        mock_pseudo.assert_called_once_with("encrypt", resource, element, {})
        self.assertEqual(result, [])


class TestDispatchPass1FhirpathErrorSkip(unittest.TestCase):
    """In 'skip' mode, FHIRPath evaluation errors trigger a safety fallback redact."""

    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_fhirpath_cached", side_effect=RuntimeError("parse error"))
    @patch(f"{_MOD}._classify_match", return_value="fhirpath")
    @patch(f"{_MOD}._build_match_candidates")
    def test_fhirpath_error_skip_mode(
        self, mock_candidates, mock_classify, mock_eval_fp,
        mock_params, mock_deident,
    ):
        resource = {"resourceType": "Patient"}
        rule = _make_rule("complex-rule", "Patient.name.where(use='official')", "redact")
        mock_candidates.return_value = ["Patient.name.where(use='official')"]

        # Should NOT raise
        result = dispatch_pass1(resource, [rule], {}, [], "skip")

        mock_eval_fp.assert_called_once()
        # Fail-safe: when FHIRPath eval fails in skip mode, a fallback redact
        # is applied to the candidate path to prevent PHI leaking through.
        mock_deident.assert_called_once_with(
            "redact", resource,
            {"path": "Patient.name.where(use='official')", "value": None}, {},
        )
        self.assertEqual(result, [])


class TestDispatchPass1FhirpathErrorRaise(unittest.TestCase):
    """In 'raise' mode, FHIRPath evaluation errors propagate."""

    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_fhirpath_cached", side_effect=RuntimeError("parse error"))
    @patch(f"{_MOD}._classify_match", return_value="fhirpath")
    @patch(f"{_MOD}._build_match_candidates")
    def test_fhirpath_error_raise_mode(
        self, mock_candidates, mock_classify, mock_eval_fp, mock_params,
    ):
        resource = {"resourceType": "Patient"}
        rule = _make_rule("complex-rule", "Patient.name.where(use='official')", "redact")
        mock_candidates.return_value = ["Patient.name.where(use='official')"]

        with self.assertRaises(RuntimeError) as ctx:
            dispatch_pass1(resource, [rule], {}, [], "raise")

        self.assertIn("parse error", str(ctx.exception))


class TestDispatchPass1ActionFailureSkipMode(unittest.TestCase):
    """Action failure in 'skip' mode triggers fallback redact."""

    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_action_failure_falls_back_to_redact(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident,
    ):
        resource = {"resourceType": "Patient", "name": [{"family": "Doe"}]}
        rule = _make_rule("hash-name", "Patient.name", "cryptohash")

        element = _make_element("Patient.name", [{"family": "Doe"}])
        mock_candidates.return_value = ["Patient.name"]
        mock_eval_simple.return_value = [element]

        # First call (cryptohash) raises, second call (redact fallback) succeeds
        mock_deident.side_effect = [ValueError("no key"), None]

        result = dispatch_pass1(resource, [rule], {}, [], "skip")

        self.assertEqual(mock_deident.call_count, 2)
        first_call, second_call = mock_deident.call_args_list
        self.assertEqual(first_call, call("cryptohash", resource, element, {}))
        self.assertEqual(second_call, call("redact", resource, element, {}))
        self.assertEqual(result, [])


class TestDispatchPass1ManifestTracking(unittest.TestCase):
    """With _MANIFEST_ENABLED=True, manifest_entries is populated after action."""

    @patch(f"{_MOD}._MANIFEST_ENABLED", True)
    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_manifest_entries_populated(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident,
    ):
        resource = {"resourceType": "Patient", "birthDate": "1990-01-01"}
        rule = _make_rule("redact-dob", "Patient.birthDate", "redact")

        element = _make_element("Patient.birthDate", "1990-01-01")
        mock_candidates.return_value = ["Patient.birthDate"]
        mock_eval_simple.return_value = [element]

        manifest = []
        dispatch_pass1(resource, [rule], {}, manifest, "skip")

        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["rule"], "redact-dob")
        self.assertEqual(manifest[0]["action"], "redact")
        self.assertEqual(manifest[0]["path"], "Patient.birthDate")

    @patch(f"{_MOD}._MANIFEST_ENABLED", True)
    @patch(f"{_MOD}.perform_deidentification")
    @patch(f"{_MOD}._resolve_rule_params", return_value={})
    @patch(f"{_MOD}._evaluate_simple_path")
    @patch(f"{_MOD}._classify_match", return_value="simple")
    @patch(f"{_MOD}._build_match_candidates")
    def test_manifest_records_fallback_action_on_failure(
        self, mock_candidates, mock_classify, mock_eval_simple,
        mock_params, mock_deident,
    ):
        """When an action fails in skip mode, manifest records 'redact' not the original."""
        resource = {"resourceType": "Patient", "name": [{"family": "Doe"}]}
        rule = _make_rule("hash-name", "Patient.name", "cryptohash")

        element = _make_element("Patient.name", [{"family": "Doe"}])
        mock_candidates.return_value = ["Patient.name"]
        mock_eval_simple.return_value = [element]

        # cryptohash fails, redact fallback succeeds
        mock_deident.side_effect = [ValueError("no key"), None]

        manifest = []
        dispatch_pass1(resource, [rule], {}, manifest, "skip")

        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["action"], "redact")
        self.assertEqual(manifest[0]["rule"], "hash-name")


if __name__ == "__main__":
    unittest.main()
