import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import unittest
import re

from pipeline.post_processor import (
    _rewrite_references,
    _collect_reference_ids,
    _pseudonymize_reference_string,
    _rewrite_text_ids,
    _build_text_id_matcher,
    _aho_replace,
    _post_process_resource,
    _MAX_NESTING_DEPTH,
    _build_text_id_automaton,
)

try:
    import ahocorasick
    HAS_AHO = True
except ImportError:
    HAS_AHO = False


class TestRewriteReferences(unittest.TestCase):

    def test_simple_dict_reference_rewrite(self):
        obj = {"reference": "Patient/123"}
        _rewrite_references(obj, {"Patient/123": "Patient/abc"})
        self.assertEqual(obj["reference"], "Patient/abc")

    def test_nested_references_in_deep_structure(self):
        obj = {
            "subject": {"reference": "Patient/1"},
            "encounter": {
                "inner": {"reference": "Encounter/2"},
            },
        }
        ref_map = {"Patient/1": "Patient/a", "Encounter/2": "Encounter/b"}
        _rewrite_references(obj, ref_map)
        self.assertEqual(obj["subject"]["reference"], "Patient/a")
        self.assertEqual(obj["encounter"]["inner"]["reference"], "Encounter/b")

    def test_list_of_references(self):
        obj = [
            {"reference": "Patient/1"},
            {"reference": "Patient/2"},
        ]
        ref_map = {"Patient/1": "Patient/a", "Patient/2": "Patient/b"}
        _rewrite_references(obj, ref_map)
        self.assertEqual(obj[0]["reference"], "Patient/a")
        self.assertEqual(obj[1]["reference"], "Patient/b")

    def test_noop_when_ref_not_in_map(self):
        obj = {"reference": "Patient/999"}
        _rewrite_references(obj, {"Patient/1": "Patient/a"})
        self.assertEqual(obj["reference"], "Patient/999")

    def test_url_field_also_rewritten(self):
        obj = {"url": "Patient/123"}
        _rewrite_references(obj, {"Patient/123": "Patient/abc"})
        self.assertEqual(obj["url"], "Patient/abc")

    def test_non_reference_field_not_rewritten(self):
        obj = {"display": "Patient/123"}
        _rewrite_references(obj, {"Patient/123": "Patient/abc"})
        self.assertEqual(obj["display"], "Patient/123")


class TestCollectReferenceIds(unittest.TestCase):

    def test_collects_type_id_references(self):
        obj = {"reference": "Patient/123"}
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, {"123"})

    def test_collects_urn_uuid_references(self):
        obj = {"reference": "urn:uuid:abc-def"}
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, {"abc-def"})

    def test_skips_contained_refs(self):
        obj = {"reference": "#contained-id"}
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, set())

    def test_skips_query_refs(self):
        obj = {"reference": "Patient?name=John"}
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, set())

    def test_skips_http_urls(self):
        obj = {"reference": "http://example.com/Patient/1"}
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, set())

    def test_collects_from_nested_structure(self):
        obj = {
            "subject": {"reference": "Patient/1"},
            "items": [
                {"reference": "Observation/2"},
                {"reference": "urn:uuid:xyz"},
            ],
        }
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, {"1", "2", "xyz"})


class TestPseudonymizeReferenceString(unittest.TestCase):

    def test_type_id_format(self):
        result = _pseudonymize_reference_string("Patient/123", {"123": "pseudo123"})
        self.assertEqual(result, "Patient/pseudo123")

    def test_urn_uuid_format(self):
        result = _pseudonymize_reference_string("urn:uuid:abc", {"abc": "pseudo-abc"})
        self.assertEqual(result, "urn:uuid:pseudo-abc")

    def test_skip_http_urls(self):
        result = _pseudonymize_reference_string(
            "http://example.com/Patient/1", {"1": "pseudo1"}
        )
        self.assertEqual(result, "http://example.com/Patient/1")

    def test_skip_contained_refs(self):
        result = _pseudonymize_reference_string("#contained", {"contained": "x"})
        self.assertEqual(result, "#contained")

    def test_skip_query_refs(self):
        result = _pseudonymize_reference_string("Patient?name=John", {"John": "x"})
        self.assertEqual(result, "Patient?name=John")

    def test_id_not_in_mapping_returns_unchanged(self):
        result = _pseudonymize_reference_string("Patient/999", {"123": "pseudo123"})
        self.assertEqual(result, "Patient/999")

    def test_empty_string_returns_unchanged(self):
        result = _pseudonymize_reference_string("", {"a": "b"})
        self.assertEqual(result, "")

    def test_none_returns_none(self):
        result = _pseudonymize_reference_string(None, {"a": "b"})
        self.assertIsNone(result)


class TestRewriteTextIds(unittest.TestCase):

    def test_replace_bare_ids_in_free_text(self):
        obj = {"text": "The patient ID is abc123 in the record"}
        _rewrite_text_ids(obj, {"abc123": "pseudo1"})
        self.assertEqual(obj["text"], "The patient ID is pseudo1 in the record")

    def test_skip_structural_fields(self):
        obj = {
            "id": "abc123",
            "reference": "Patient/abc123",
            "url": "abc123",
            "resourceType": "abc123",
            "text": "abc123",
        }
        _rewrite_text_ids(obj, {"abc123": "pseudo1"})
        self.assertEqual(obj["id"], "abc123")
        self.assertEqual(obj["reference"], "Patient/abc123")
        self.assertEqual(obj["url"], "abc123")
        self.assertEqual(obj["resourceType"], "abc123")
        self.assertEqual(obj["text"], "pseudo1")

    def test_handle_lists_of_strings(self):
        obj = ["abc123 is here", "no match", "abc123 again"]
        _rewrite_text_ids(obj, {"abc123": "pseudo1"})
        self.assertEqual(obj[0], "pseudo1 is here")
        self.assertEqual(obj[1], "no match")
        self.assertEqual(obj[2], "pseudo1 again")

    def test_empty_id_map_is_noop(self):
        obj = {"text": "some text"}
        _rewrite_text_ids(obj, {})
        self.assertEqual(obj["text"], "some text")

    def test_nested_dict_replacement(self):
        obj = {"outer": {"inner": {"note": "ID is abc123"}}}
        _rewrite_text_ids(obj, {"abc123": "pseudo1"})
        self.assertEqual(obj["outer"]["inner"]["note"], "ID is pseudo1")


class TestBuildTextIdMatcher(unittest.TestCase):

    def test_empty_id_map_returns_none_none(self):
        automaton, compiled = _build_text_id_matcher({})
        self.assertIsNone(automaton)
        self.assertIsNone(compiled)

    def test_nonempty_map_returns_something(self):
        automaton, compiled = _build_text_id_matcher({"abc": "xyz"})
        if HAS_AHO:
            self.assertIsNotNone(automaton)
            self.assertIsNone(compiled)
        else:
            self.assertIsNone(automaton)
            self.assertIsNotNone(compiled)

    def test_regex_contains_word_boundaries(self):
        if HAS_AHO:
            self.skipTest("ahocorasick installed; regex path not used")
        _, compiled = _build_text_id_matcher({"abc": "xyz"})
        self.assertIn(r"\b", compiled.pattern)

    def test_map_with_empty_key_skipped(self):
        automaton, compiled = _build_text_id_matcher({"": "xyz", "abc": "123"})
        if not HAS_AHO:
            self.assertIsNotNone(compiled)
            self.assertNotIn("||", compiled.pattern)

    def test_only_empty_keys_returns_none_none(self):
        automaton, compiled = _build_text_id_matcher({"": "xyz"})
        if HAS_AHO:
            # ahocorasick skips empty keys in _build_text_id_automaton
            pass
        else:
            self.assertIsNone(automaton)
            self.assertIsNone(compiled)


@unittest.skipUnless(HAS_AHO, "pyahocorasick not installed")
class TestAhoReplace(unittest.TestCase):

    def _make_automaton(self, id_map):
        return _build_text_id_automaton(id_map)

    def test_word_boundary_matching(self):
        id_map = {"abc": "XYZ"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("xabcd", automaton, id_map)
        self.assertEqual(result, "xabcd")

    def test_word_boundary_match_at_start(self):
        id_map = {"abc": "XYZ"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("abc end", automaton, id_map)
        self.assertEqual(result, "XYZ end")

    def test_word_boundary_match_at_end(self):
        id_map = {"abc": "XYZ"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("start abc", automaton, id_map)
        self.assertEqual(result, "start XYZ")

    def test_multiple_replacements(self):
        id_map = {"abc": "X", "def": "Y"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("abc and def", automaton, id_map)
        self.assertEqual(result, "X and Y")

    def test_no_match_returns_original(self):
        id_map = {"abc": "XYZ"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("nothing here", automaton, id_map)
        self.assertEqual(result, "nothing here")

    def test_standalone_word(self):
        id_map = {"abc": "XYZ"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("abc", automaton, id_map)
        self.assertEqual(result, "XYZ")

    def test_no_replace_inside_longer_word(self):
        id_map = {"id": "XX"}
        automaton = self._make_automaton(id_map)
        result = _aho_replace("void identity", automaton, id_map)
        self.assertEqual(result, "void identity")


class TestPostProcessResource(unittest.TestCase):

    def test_combined_ref_and_text_replacement(self):
        obj = {
            "subject": {"reference": "Patient/123"},
            "note": "Patient 123 was seen",
        }
        ref_mapping = {"123": "pseudo1"}
        id_map = {"123": "pseudo1"}
        automaton, compiled = _build_text_id_matcher(id_map)
        _post_process_resource(obj, ref_mapping, id_map, automaton=automaton, compiled=compiled)
        self.assertEqual(obj["subject"]["reference"], "Patient/pseudo1")
        self.assertEqual(obj["note"], "Patient pseudo1 was seen")

    def test_none_ref_mapping_skips_reference_rewriting(self):
        obj = {"subject": {"reference": "Patient/123"}}
        _post_process_resource(obj, None, None)
        self.assertEqual(obj["subject"]["reference"], "Patient/123")

    def test_none_id_map_skips_text_replacement(self):
        obj = {"note": "Patient 123 was seen"}
        _post_process_resource(obj, None, None)
        self.assertEqual(obj["note"], "Patient 123 was seen")

    def test_ref_mapping_only(self):
        obj = {
            "subject": {"reference": "Patient/123"},
            "note": "Patient 123 was seen",
        }
        ref_mapping = {"123": "pseudo1"}
        _post_process_resource(obj, ref_mapping, None)
        self.assertEqual(obj["subject"]["reference"], "Patient/pseudo1")
        self.assertEqual(obj["note"], "Patient 123 was seen")

    def test_id_map_only(self):
        obj = {
            "subject": {"reference": "Patient/123"},
            "note": "Patient 123 was seen",
        }
        id_map = {"123": "pseudo1"}
        automaton, compiled = _build_text_id_matcher(id_map)
        _post_process_resource(obj, None, id_map, automaton=automaton, compiled=compiled)
        self.assertEqual(obj["subject"]["reference"], "Patient/123")
        self.assertEqual(obj["note"], "Patient pseudo1 was seen")

    def test_list_string_replacement(self):
        obj = [{"note": "ID 123"}, "bare 123 text"]
        id_map = {"123": "pseudo1"}
        automaton, compiled = _build_text_id_matcher(id_map)
        _post_process_resource(obj, None, id_map, automaton=automaton, compiled=compiled)
        self.assertEqual(obj[0]["note"], "ID pseudo1")
        self.assertEqual(obj[1], "bare pseudo1 text")

    def test_display_deleted_when_ref_pseudonymized(self):
        obj = {"reference": "Patient/123", "display": "John Doe"}
        ref_mapping = {"123": "pseudo1"}
        _post_process_resource(obj, ref_mapping, None)
        self.assertEqual(obj["reference"], "Patient/pseudo1")
        self.assertNotIn("display", obj)


class TestMaxNestingDepth(unittest.TestCase):

    def _build_nested(self, depth):
        obj = {"leaf": "value"}
        for _ in range(depth):
            obj = {"child": obj}
        return obj

    def test_rewrite_references_raises_at_max_depth(self):
        obj = self._build_nested(_MAX_NESTING_DEPTH + 1)
        with self.assertRaises(ValueError) as ctx:
            _rewrite_references(obj, {"x": "y"})
        self.assertIn(str(_MAX_NESTING_DEPTH), str(ctx.exception))

    def test_rewrite_text_ids_raises_at_max_depth(self):
        obj = self._build_nested(_MAX_NESTING_DEPTH + 1)
        with self.assertRaises(ValueError) as ctx:
            _rewrite_text_ids(obj, {"abc": "xyz"})
        self.assertIn(str(_MAX_NESTING_DEPTH), str(ctx.exception))

    def test_post_process_resource_raises_at_max_depth(self):
        obj = self._build_nested(_MAX_NESTING_DEPTH + 1)
        with self.assertRaises(ValueError) as ctx:
            _post_process_resource(obj, {"123": "pseudo"}, {"abc": "xyz"})
        self.assertIn(str(_MAX_NESTING_DEPTH), str(ctx.exception))

    def test_rewrite_references_ok_just_under_max_depth(self):
        obj = self._build_nested(_MAX_NESTING_DEPTH - 1)
        _rewrite_references(obj, {"x": "y"})

    def test_collect_reference_ids_silently_stops_at_max_depth(self):
        obj = self._build_nested(_MAX_NESTING_DEPTH + 1)
        ids = set()
        _collect_reference_ids(obj, ids)
        self.assertEqual(ids, set())


if __name__ == "__main__":
    unittest.main()
