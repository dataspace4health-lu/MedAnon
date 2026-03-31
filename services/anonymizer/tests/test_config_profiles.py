"""Tests for HIPAA Safe Harbor and Research Pseudonymous config profiles.

These tests validate the YAML rule structure of the two new compliance profiles
without running process_data (no fhirpathpy dependency — runs locally without Docker).

Two complementary approaches:
  1. Structural tests — verify the YAML loads and contains the expected rules
  2. Integration tests (marked) — verify actual processing output (requires Docker)
"""
import os
import unittest

import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


def _load_config(name: str) -> dict:
    """Load a config YAML and return the parsed dict."""
    path = os.path.join(_CONFIG_DIR, name)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _rules(config: dict) -> list[dict]:
    return config.get("rules") or []


def _matches_for_action(rules: list[dict], action: str) -> list[str]:
    """Return all FHIRPath match expressions that use the given action."""
    return [r["match"] for r in rules if r.get("action") == action]


def _rule_for_match(rules: list[dict], match: str) -> dict | None:
    """Return the first rule whose match equals *match*, or None."""
    for r in rules:
        if r.get("match") == match:
            return r
    return None


# ---------------------------------------------------------------------------
# TestHipaaProfile — structural validation
# ---------------------------------------------------------------------------

class TestHipaaProfile(unittest.TestCase):
    """Validates config_hipaa_safe_harbor.yaml rule structure."""

    def setUp(self):
        self.cfg = _load_config("config_hipaa_safe_harbor.yaml")
        self.rules = _rules(self.cfg)

    # ── General section ──────────────────────────────────────────────────

    def test_loads_without_error(self):
        self.assertIsInstance(self.cfg, dict)

    def test_has_general_section(self):
        self.assertIn("general", self.cfg)

    def test_has_rules(self):
        self.assertGreater(len(self.rules), 0, "Profile must have at least one rule")

    # ── Identifier 1: Names are redacted ─────────────────────────────────

    def test_patient_name_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.name")
        self.assertIsNotNone(rule, "Patient.name rule must exist")
        self.assertEqual(rule["action"], "redact")

    def test_patient_contact_name_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.contact.name")
        self.assertIsNotNone(rule, "Patient.contact.name rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── Identifiers 4-6: Telecom is redacted ─────────────────────────────

    def test_patient_telecom_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.telecom")
        self.assertIsNotNone(rule, "Patient.telecom rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── Identifiers 7-11: Administrative identifiers are redacted ─────────

    def test_patient_identifier_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.identifier")
        self.assertIsNotNone(rule, "Patient.identifier rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── Identifier 3: Dates generalized to year only ─────────────────────

    def test_birth_date_uses_date_year_strategy(self):
        rule = _rule_for_match(self.rules, "Patient.birthDate")
        self.assertIsNotNone(rule, "Patient.birthDate rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(rule["params"]["strategy"], "date_year")

    def test_effective_date_time_uses_date_year(self):
        rule = _rule_for_match(self.rules, '"*.effectiveDateTime"') or \
               _rule_for_match(self.rules, "*.effectiveDateTime")
        self.assertIsNotNone(rule, "*.effectiveDateTime rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(rule["params"]["strategy"], "date_year")

    # ── Identifier 2: Geographic data below state level ───────────────────

    def test_postal_code_uses_zip_prefix(self):
        rule = _rule_for_match(self.rules, "Patient.address.postalCode")
        self.assertIsNotNone(rule, "Patient.address.postalCode rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(rule["params"]["strategy"], "zip_prefix")

    def test_address_line_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.address.line")
        self.assertIsNotNone(rule, "Patient.address.line rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── Identifier 16-17: Biometrics and photos ───────────────────────────

    def test_patient_photo_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.photo")
        self.assertIsNotNone(rule, "Patient.photo rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── Cross-resource: IDs are hashed (not redacted) ─────────────────────

    def test_all_ids_are_cryptohashed(self):
        rule = _rule_for_match(self.rules, '"*.id"') or \
               _rule_for_match(self.rules, "*.id")
        self.assertIsNotNone(rule, '"*.id" rule must exist')
        self.assertEqual(rule["action"], "cryptohash")

    # ── Text scrubbing present ────────────────────────────────────────────

    def test_text_scrubbing_present(self):
        scrub_matches = _matches_for_action(self.rules, "scrub_text")
        self.assertGreater(len(scrub_matches), 0, "At least one scrub_text rule required")


# ---------------------------------------------------------------------------
# TestResearchProfile — structural validation
# ---------------------------------------------------------------------------

class TestResearchProfile(unittest.TestCase):
    """Validates config_research_pseudonymous.yaml rule structure."""

    def setUp(self):
        self.cfg = _load_config("config_research_pseudonymous.yaml")
        self.rules = _rules(self.cfg)

    # ── General section ──────────────────────────────────────────────────

    def test_loads_without_error(self):
        self.assertIsInstance(self.cfg, dict)

    def test_has_rules(self):
        self.assertGreater(len(self.rules), 0)

    # ── Names always redacted ─────────────────────────────────────────────

    def test_patient_name_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.name")
        self.assertIsNotNone(rule, "Patient.name rule must exist")
        self.assertEqual(rule["action"], "redact")

    def test_patient_telecom_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.telecom")
        self.assertIsNotNone(rule, "Patient.telecom rule must exist")
        self.assertEqual(rule["action"], "redact")

    def test_patient_identifier_is_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.identifier")
        self.assertIsNotNone(rule, "Patient.identifier rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── Dates: year-month (finer than HIPAA Safe Harbor) ─────────────────

    def test_birth_date_uses_date_year_month(self):
        rule = _rule_for_match(self.rules, "Patient.birthDate")
        self.assertIsNotNone(rule, "Patient.birthDate rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(
            rule["params"]["strategy"], "date_year_month",
            "Research profile must use year-month (not year-only) for birthDate",
        )

    def test_effective_date_time_uses_date_year_month(self):
        rule = _rule_for_match(self.rules, '"*.effectiveDateTime"') or \
               _rule_for_match(self.rules, "*.effectiveDateTime")
        self.assertIsNotNone(rule, "*.effectiveDateTime rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(rule["params"]["strategy"], "date_year_month")

    # ── Key difference: dates are year-month here vs year-only in HIPAA ───

    def test_research_date_strategy_differs_from_hipaa(self):
        hipaa = _load_config("config_hipaa_safe_harbor.yaml")
        hipaa_bd_rule = _rule_for_match(_rules(hipaa), "Patient.birthDate")
        research_bd_rule = _rule_for_match(self.rules, "Patient.birthDate")
        self.assertNotEqual(
            hipaa_bd_rule["params"]["strategy"],
            research_bd_rule["params"]["strategy"],
            "Research and HIPAA profiles must use different date strategies",
        )

    # ── Geographic: zip prefix retained ──────────────────────────────────

    def test_postal_code_uses_zip_prefix(self):
        rule = _rule_for_match(self.rules, "Patient.address.postalCode")
        self.assertIsNotNone(rule, "Patient.address.postalCode rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(rule["params"]["strategy"], "zip_prefix")

    # ── IDs pseudonymized (cryptohash), not redacted ──────────────────────

    def test_ids_are_cryptohashed(self):
        rule = _rule_for_match(self.rules, '"*.id"') or \
               _rule_for_match(self.rules, "*.id")
        self.assertIsNotNone(rule, '"*.id" rule must exist')
        self.assertEqual(
            rule["action"], "cryptohash",
            "Research profile must pseudonymize IDs (cryptohash) to preserve referential integrity",
        )

    # ── Text scrubbing present ────────────────────────────────────────────

    def test_text_scrubbing_present(self):
        scrub_matches = _matches_for_action(self.rules, "scrub_text")
        self.assertGreater(len(scrub_matches), 0, "At least one scrub_text rule required")


# ---------------------------------------------------------------------------
# TestProfileComparison — cross-profile invariants
# ---------------------------------------------------------------------------

class TestProfileComparison(unittest.TestCase):
    """Cross-profile consistency checks."""

    def setUp(self):
        self.hipaa = _load_config("config_hipaa_safe_harbor.yaml")
        self.research = _load_config("config_research_pseudonymous.yaml")
        self.gdpr = _load_config("config_gdpr_eu.yaml")

    def test_all_profiles_handle_patient_name(self):
        """Every profile must have a Patient.name rule.
        HIPAA/Research redact names; GDPR pseudonymizes (cryptohash) per Art. 4(5)."""
        allowed = {"redact", "cryptohash"}
        for cfg_name, cfg in [("hipaa", self.hipaa), ("research", self.research), ("gdpr", self.gdpr)]:
            rule = _rule_for_match(_rules(cfg), "Patient.name")
            self.assertIsNotNone(rule, f"{cfg_name}: Patient.name rule missing")
            self.assertIn(
                rule["action"], allowed,
                f"{cfg_name}: Patient.name must use redact or cryptohash, got {rule['action']}",
            )

    def test_all_profiles_have_id_rule(self):
        for cfg_name, cfg in [("hipaa", self.hipaa), ("research", self.research)]:
            rule = _rule_for_match(_rules(cfg), '"*.id"') or \
                   _rule_for_match(_rules(cfg), "*.id")
            self.assertIsNotNone(rule, f"{cfg_name}: *.id rule missing")

    def test_hipaa_and_research_both_cover_telecom(self):
        for cfg_name, cfg in [("hipaa", self.hipaa), ("research", self.research)]:
            rule = _rule_for_match(_rules(cfg), "Patient.telecom")
            self.assertIsNotNone(rule, f"{cfg_name}: Patient.telecom missing")
            self.assertEqual(rule["action"], "redact")


# ---------------------------------------------------------------------------
# TestStructuralProfile — structure-preserving de-identification
# ---------------------------------------------------------------------------

class TestStructuralProfile(unittest.TestCase):
    """Validates config_structure_preserving.yaml rule structure."""

    def setUp(self):
        self.cfg = _load_config("config_structure_preserving.yaml")
        self.rules = _rules(self.cfg)

    def test_loads_without_error(self):
        self.assertIsInstance(self.cfg, dict)

    def test_rewrite_references_enabled(self):
        """rewrite_references must be true so bundle cross-references stay consistent."""
        self.assertTrue(
            self.cfg.get("general", {}).get("rewrite_references"),
            "structural profile must set rewrite_references: true",
        )

    def test_has_rules(self):
        self.assertGreater(len(self.rules), 0)

    # ── IDs pseudonymized via gPAS ────────────────────────────────────────

    def test_all_ids_use_gpas_pseudonymize(self):
        rule = _rule_for_match(self.rules, '"*.id"') or \
               _rule_for_match(self.rules, "*.id")
        self.assertIsNotNone(rule, '"*.id" rule must exist')
        self.assertEqual(rule["action"], "gpas_pseudonymize")

    def test_identifier_values_use_gpas_pseudonymize(self):
        rule = _rule_for_match(self.rules, '"*.identifier.value"') or \
               _rule_for_match(self.rules, "*.identifier.value")
        self.assertIsNotNone(rule, '"*.identifier.value" rule must exist')
        self.assertEqual(rule["action"], "gpas_pseudonymize")

    # ── Name sub-fields substituted (NOT redacted) ────────────────────────

    def test_patient_name_family_substituted(self):
        rule = _rule_for_match(self.rules, "Patient.name.family")
        self.assertIsNotNone(rule, "Patient.name.family rule must exist")
        self.assertEqual(rule["action"], "substitute",
                         "structural profile keeps fields present — must use substitute, not redact")
        self.assertEqual(rule["params"]["substitute_with"], "[REDACTED]")

    def test_patient_name_given_substituted(self):
        rule = _rule_for_match(self.rules, "Patient.name.given")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["action"], "substitute")

    def test_no_rule_removes_patient_name_entirely(self):
        """Must NOT have a rule that redacts the whole Patient.name array."""
        rule = _rule_for_match(self.rules, "Patient.name")
        if rule:
            self.assertNotEqual(rule["action"], "redact",
                                "structural profile must not delete Patient.name — use substitute on sub-fields")

    # ── PII text substituted, not redacted ────────────────────────────────

    def test_telecom_value_substituted(self):
        rule = _rule_for_match(self.rules, "Patient.telecom.value")
        self.assertIsNotNone(rule, "Patient.telecom.value rule must exist")
        self.assertEqual(rule["action"], "substitute")

    def test_address_line_substituted(self):
        rule = _rule_for_match(self.rules, "Patient.address.line")
        self.assertIsNotNone(rule)
        self.assertEqual(rule["action"], "substitute",
                         "structural profile must substitute address.line, not redact it")

    # ── birthDate generalised to year only ────────────────────────────────

    def test_birth_date_uses_date_year(self):
        rule = _rule_for_match(self.rules, "Patient.birthDate")
        self.assertIsNotNone(rule, "Patient.birthDate rule must exist")
        self.assertEqual(rule["action"], "generalize")
        self.assertEqual(rule["params"]["strategy"], "date_year")

    # ── Binary blobs redacted (no text substitute available) ──────────────

    def test_patient_photo_redacted(self):
        rule = _rule_for_match(self.rules, "Patient.photo")
        self.assertIsNotNone(rule, "Patient.photo rule must exist")
        self.assertEqual(rule["action"], "redact")

    # ── No explicit reference pseudonymization rules ──────────────────────

    def test_no_subject_reference_pseudonymize_rule(self):
        """References must be handled by rewrite_references, not an explicit gpas rule.

        An explicit gpas_pseudonymize on *.subject.reference pseudonymizes the
        full string 'Patient/ID' and loses the resource type prefix.
        """
        rule = _rule_for_match(self.rules, '"*.subject.reference"') or \
               _rule_for_match(self.rules, "*.subject.reference")
        if rule:
            self.assertNotEqual(rule["action"], "gpas_pseudonymize",
                                "Do not pseudonymize *.subject.reference directly — use rewrite_references: true")

    # ── Key difference from HIPAA: fields stay present ────────────────────

    def test_differs_from_hipaa_on_name(self):
        """HIPAA redacts Patient.name entirely; structural profile substitutes sub-fields."""
        hipaa = _load_config("config_hipaa_safe_harbor.yaml")
        hipaa_rule = _rule_for_match(_rules(hipaa), "Patient.name")
        self.assertIsNotNone(hipaa_rule)
        self.assertEqual(hipaa_rule["action"], "redact")

        # Structural has no whole-name redact rule
        structural_whole = _rule_for_match(self.rules, "Patient.name")
        self.assertTrue(
            structural_whole is None or structural_whole["action"] != "redact",
            "structural profile must not redact Patient.name (HIPAA does that; structural substitutes sub-fields)",
        )


# ---------------------------------------------------------------------------
# TestConfigServiceCache — caching behaviour of pipeline.config_service
# ---------------------------------------------------------------------------

class TestConfigServiceCache(unittest.TestCase):
    """Tests for get_settings() caching behaviour in pipeline.config_service.

    These tests verify three properties:
      1. 'auto' is re-resolved on every call (not cached under the alias).
      2. clear_settings_cache() forces a cache miss on the next call.
      3. MEDANON_CONFIG_CACHE_TTL triggers automatic cache invalidation.
    """

    _LOCAL_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")

    def setUp(self):
        import pipeline.config_service as cs
        self._cs = cs
        # Point to the local config directory so calls don't need Docker (/code/config).
        self._orig_config_dir = cs._CONFIG_DIR
        cs._CONFIG_DIR = self._LOCAL_CONFIG_DIR
        cs.clear_settings_cache()

    def tearDown(self):
        self._cs._CONFIG_DIR = self._orig_config_dir
        self._cs.clear_settings_cache()
        os.environ.pop('GPAS_URL', None)

    def test_auto_profile_resolves_per_call(self):
        """'auto' must re-resolve on every call without requiring a cache clear."""
        os.environ.pop('GPAS_URL', None)
        expected_default = os.path.join(self._LOCAL_CONFIG_DIR, 'config.yaml')
        expected_gpas = os.path.join(self._LOCAL_CONFIG_DIR, 'config_gpas.yaml')
        self.assertEqual(self._cs._resolve_profile('auto'), expected_default)
        os.environ['GPAS_URL'] = 'http://gpas:8080'
        self.assertEqual(self._cs._resolve_profile('auto'), expected_gpas)

    def test_clear_cache_forces_reload(self):
        """clear_settings_cache() must cause the next get_settings() call to be a miss."""
        self._cs.get_settings('minimal')
        self.assertEqual(self._cs._load_settings.cache_info().currsize, 1)

        self._cs.clear_settings_cache()
        self.assertEqual(self._cs._load_settings.cache_info().currsize, 0,
                         "Cache should be empty after clear")

        self._cs.get_settings('minimal')
        self.assertEqual(self._cs._load_settings.cache_info().misses, 1,
                         "Call after clear must be a fresh cache miss")

    def test_ttl_clears_cache(self):
        """When _CACHE_TTL > 0, an expired _last_clear triggers automatic invalidation."""
        import time
        orig_ttl = self._cs._CACHE_TTL
        try:
            # Pre-populate the cache with one entry.
            self._cs.get_settings('minimal')
            self.assertEqual(self._cs._load_settings.cache_info().currsize, 1)

            # Enable TTL of 1 s and backdate _last_clear by 2 s to simulate expiry.
            self._cs._CACHE_TTL = 1
            self._cs._last_clear = time.monotonic() - 2

            # Next call must auto-clear and reload (1 fresh miss).
            self._cs.get_settings('minimal')
            self.assertEqual(self._cs._load_settings.cache_info().misses, 1,
                             "TTL-triggered clear must produce a fresh cache miss")
        finally:
            self._cs._CACHE_TTL = orig_ttl


if __name__ == "__main__":
    unittest.main()
