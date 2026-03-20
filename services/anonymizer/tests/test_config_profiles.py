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


if __name__ == "__main__":
    unittest.main()
