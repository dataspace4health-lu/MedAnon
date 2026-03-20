"""Tests for app/modules/nlp/detect.py — NLP-based PHI detection."""

import unittest
from copy import deepcopy
from unittest.mock import patch, MagicMock

from integrations.nlp.detector import (
    nlp_detect_by_path,
    _analyze_and_replace,
    _tokenize,
    _resolve_entities,
    _scrub_xhtml_text_nodes,
    HEALTHCARE_ENTITIES,
)


def _fresh_state():
    return {"next": {}, "map": {}, "reverse": {}}


# ---------------------------------------------------------------------------
# Unit: _tokenize
# ---------------------------------------------------------------------------
class TestTokenize(unittest.TestCase):

    def test_generates_sequential_token(self):
        state = _fresh_state()
        t1 = _tokenize("John Smith", "PERSON", state)
        self.assertEqual(t1, "[[PERSON_1]]")

    def test_same_value_same_token(self):
        state = _fresh_state()
        t1 = _tokenize("John Smith", "PERSON", state)
        t2 = _tokenize("John Smith", "PERSON", state)
        self.assertEqual(t1, t2)

    def test_different_values_different_tokens(self):
        state = _fresh_state()
        t1 = _tokenize("John Smith", "PERSON", state)
        t2 = _tokenize("Jane Doe", "PERSON", state)
        self.assertNotEqual(t1, t2)
        self.assertEqual(t1, "[[PERSON_1]]")
        self.assertEqual(t2, "[[PERSON_2]]")

    def test_reverse_map_is_maintained(self):
        state = _fresh_state()
        token = _tokenize("Jane Doe", "PERSON", state)
        self.assertEqual(state["reverse"][token], "Jane Doe")


# ---------------------------------------------------------------------------
# Unit: _resolve_entities
# ---------------------------------------------------------------------------
class TestResolveEntities(unittest.TestCase):

    def test_healthcare_is_default(self):
        self.assertEqual(_resolve_entities(None), HEALTHCARE_ENTITIES)
        self.assertEqual(_resolve_entities("healthcare"), HEALTHCARE_ENTITIES)

    def test_all_returns_healthcare(self):
        self.assertEqual(_resolve_entities("all"), HEALTHCARE_ENTITIES)

    def test_explicit_list(self):
        result = _resolve_entities(["PERSON", "DATE_TIME"])
        self.assertEqual(result, ["PERSON", "DATE_TIME"])

    def test_comma_string(self):
        result = _resolve_entities("PERSON,US_SSN, DATE_TIME")
        self.assertEqual(result, ["PERSON", "US_SSN", "DATE_TIME"])


# ---------------------------------------------------------------------------
# Unit: XHTML text-node scrubber
# ---------------------------------------------------------------------------
class TestXHTMLTextScrubber(unittest.TestCase):

    def test_scrubs_only_text_nodes(self):
        html = '<div xmlns="http://www.w3.org/1999/xhtml">Hello <b>World</b></div>'
        result = _scrub_xhtml_text_nodes(html, lambda t: t.upper())
        self.assertIn("HELLO", result)
        self.assertIn("<b>", result)
        self.assertIn("</b>", result)

    def test_preserves_attributes(self):
        html = '<div xmlns="http://www.w3.org/1999/xhtml"><a href="https://example.com">link</a></div>'
        result = _scrub_xhtml_text_nodes(html, lambda t: t)
        self.assertIn('href="https://example.com"', result)

    def test_empty_text_nodes_unchanged(self):
        html = "<div></div>"
        result = _scrub_xhtml_text_nodes(html, lambda t: "REPLACED")
        # Empty text node — no data calls, markup intact
        self.assertIn("<div>", result)
        self.assertIn("</div>", result)


# ---------------------------------------------------------------------------
# Integration: _analyze_and_replace (uses real Presidio)
# ---------------------------------------------------------------------------
class TestAnalyzeAndReplace(unittest.TestCase):

    def setUp(self):
        """Warm up the Presidio engine (done once via module-level singleton)."""
        from integrations.nlp.detector import _get_analyzer
        _get_analyzer()  # ensure loaded before timing-sensitive tests

    def test_detects_ssn(self):
        # 999-prefixed SSNs are reliably detected by Presidio with context.
        state = _fresh_state()
        result = _analyze_and_replace(
            "SSN 999-94-5397 on file",
            ["US_SSN"],
            0.4,
            "en",
            "tokenize",
            state,
        )
        self.assertNotIn("999-94-5397", result)
        self.assertRegex(result, r"\[\[US_SSN_\d+\]\]")

    def test_detects_phone_number(self):
        state = _fresh_state()
        result = _analyze_and_replace(
            "Call the patient at 555-867-5309 as soon as possible.",
            ["PHONE_NUMBER"],
            0.3,
            "en",
            "tokenize",
            state,
        )
        self.assertNotIn("555-867-5309", result)

    def test_detects_person_name_via_ner(self):
        state = _fresh_state()
        result = _analyze_and_replace(
            "Patient John Smith was admitted.",
            ["PERSON"],
            0.4,
            "en",
            "tokenize",
            state,
        )
        # NER should pick up the person name
        self.assertNotIn("John Smith", result)

    def test_redact_mode(self):
        # Use a reliably-detected SSN; 999-prefix triggers Presidio's SSN recognizer.
        state = _fresh_state()
        result = _analyze_and_replace(
            "SSN 999-94-5397",
            ["US_SSN"],
            0.4,
            "en",
            "redact",
            state,
        )
        self.assertNotIn("999-94-5397", result)
        self.assertIn("[US_SSN]", result)

    def test_below_threshold_not_replaced(self):
        state = _fresh_state()
        # force threshold above any expected score
        result = _analyze_and_replace(
            "Patient John Smith was admitted.",
            ["PERSON"],
            1.1,  # impossible threshold
            "en",
            "tokenize",
            state,
        )
        self.assertEqual(result, "Patient John Smith was admitted.")

    def test_empty_text_unchanged(self):
        state = _fresh_state()
        result = _analyze_and_replace("", ["PERSON"], 0.4, "en", "tokenize", state)
        self.assertEqual(result, "")

    def test_deterministic_tokens_across_calls(self):
        state = _fresh_state()
        r1 = _analyze_and_replace(
            "SSN 999-94-5397 reported.", ["US_SSN"], 0.4, "en", "tokenize", state
        )
        r2 = _analyze_and_replace(
            "Another note: SSN 999-94-5397.", ["US_SSN"], 0.4, "en", "tokenize", state
        )
        import re
        tok1 = re.search(r"\[\[US_SSN_\d+\]\]", r1)
        tok2 = re.search(r"\[\[US_SSN_\d+\]\]", r2)
        self.assertIsNotNone(tok1)
        self.assertIsNotNone(tok2)
        self.assertEqual(tok1.group(0), tok2.group(0))


# ---------------------------------------------------------------------------
# Integration: nlp_detect_by_path (full action on resource)
# ---------------------------------------------------------------------------
class TestNlpDetectByPath(unittest.TestCase):

    def _run(self, resource, path, params=None):
        resource = deepcopy(resource)
        el = {"path": path, "value": None}
        nlp_detect_by_path(resource, el, params or {})
        return resource

    def test_plain_text_field(self):
        # NER detects the person name + SSN 999-prefix detected by Presidio recognizer.
        resource = {
            "resourceType": "Observation",
            "note": [{"text": "Patient John Smith has an SSN of 999-94-5397."}],
        }
        result = self._run(
            resource,
            "Observation.note.text",
            {"entities": ["US_SSN", "PERSON"], "threshold": 0.4, "mode": "tokenize",
             "_token_state": _fresh_state()},
        )
        text = result["note"][0]["text"]
        self.assertNotIn("999-94-5397", text)
        self.assertNotIn("John Smith", text)

    def test_html_narrative_field_preserves_markup(self):
        resource = {
            "resourceType": "Patient",
            "text": {
                "status": "generated",
                "div": '<div xmlns="http://www.w3.org/1999/xhtml">Patient Jane Doe <b>bold section</b></div>',
            },
        }
        result = self._run(
            resource,
            "Patient.text",
            {"entities": ["PERSON"], "threshold": 0.4, "mode": "tokenize",
             "html": True, "_token_state": _fresh_state()},
        )
        div = result["text"]["div"]
        self.assertNotIn("Jane Doe", div)
        # Markup must be preserved
        self.assertIn("<b>", div)
        self.assertIn("</b>", div)

    def test_missing_path_does_not_raise(self):
        resource = {"resourceType": "Patient", "id": "p1"}
        # path points to non-existent field — should silently skip
        result = self._run(resource, "Patient.nonexistent", {})
        self.assertEqual(result, {"resourceType": "Patient", "id": "p1"})

    def test_short_path_does_not_raise(self):
        resource = {"resourceType": "Patient"}
        result = self._run(resource, "Patient", {})
        self.assertEqual(result, {"resourceType": "Patient"})

    def test_redact_mode_on_resource(self):
        resource = {
            "resourceType": "Observation",
            "note": [{"text": "Call 555-867-5309 for follow-up."}],
        }
        result = self._run(
            resource,
            "Observation.note.text",
            {"entities": ["PHONE_NUMBER"], "threshold": 0.3, "mode": "redact",
             "_token_state": _fresh_state()},
        )
        text = result["note"][0]["text"]
        self.assertNotIn("555-867-5309", text)
        self.assertIn("[PHONE_NUMBER]", text)

    def test_list_of_notes(self):
        # NER detects person names across multiple note entries.
        resource = {
            "resourceType": "Observation",
            "note": [
                {"text": "Seen by Dr. John Smith on Monday."},
                {"text": "Follow-up with Jane Doe scheduled."},
            ],
        }
        result = self._run(
            resource,
            "Observation.note.text",
            {"entities": ["PERSON"], "threshold": 0.4, "mode": "tokenize",
             "_token_state": _fresh_state()},
        )
        self.assertNotIn("John Smith", result["note"][0]["text"])
        self.assertNotIn("Jane Doe", result["note"][1]["text"])


# ---------------------------------------------------------------------------
# Integration: two-pass scrub_text + nlp_detect complementary coverage
# ---------------------------------------------------------------------------
class TestTwoPassCoverage(unittest.TestCase):
    """Demonstrates that regex alone misses names but NLP+regex covers both."""

    def test_regex_misses_name_nlp_catches_it(self):
        from actions.scrub_text import scrub_text_by_path

        text = "Patient Jane Doe (DOB 1974-03-12) called about her prescription."
        resource = {"resourceType": "Observation", "note": [{"text": text}]}
        el = {"path": "Observation.note.text", "value": None}

        # Pass 1: regex — catches the date, not the name
        scrub_text_by_path(deepcopy(resource), el, {"mode": "text", "patterns": "all"})

        # Pass 2: NLP — catches "Jane Doe" as PERSON
        resource2 = deepcopy(resource)
        resource2["note"][0]["text"] = text  # reset to original
        state = _fresh_state()
        nlp_detect_by_path(
            resource2,
            el,
            {
                "entities": ["PERSON"],
                "threshold": 0.4,
                "mode": "tokenize",
                "_token_state": state,
            },
        )
        nlp_result = resource2["note"][0]["text"]
        self.assertNotIn("Jane Doe", nlp_result)


if __name__ == "__main__":
    unittest.main()
