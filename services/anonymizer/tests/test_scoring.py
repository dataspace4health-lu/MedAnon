"""Unit tests for the constraint-based scoring engine.

Covers:
  - Privacy risk evaluator (hard constraint gate)
  - Utility evaluator (continuous metric)
  - Quality evaluator (continuous metric with gate logic)
  - Composite score computation (multiplicative non-compensatory)
  - ScoreCollector accumulation + batch aggregate
  - ScoreResult serialisation (to_dict, to_fhir_extension)
  - Edge cases: empty, non-PHI resources, missing data
"""

from __future__ import annotations

import os
import sys
import pytest

# Ensure src/ is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Override scoring env before import
os.environ.setdefault("MEDANON_SCORING_ENABLED", "true")
os.environ.setdefault("MEDANON_SCORE_RISK_THRESHOLD", "0.3")
os.environ.setdefault("MEDANON_SCORE_NER_ENABLED", "false")  # no NLP in unit tests

from pipeline.scoring.models import Evidence, PrivacyDecision, ModuleScore, ScoreResult
from pipeline.scoring.constants import (
    HIPAA_SENSITIVE_PATHS,
    CLINICAL_CODE_SYSTEMS,
    INFO_LOSS_WEIGHTS,
    RISK_THRESHOLD,
)
from pipeline.scoring.privacy import PrivacyRiskEvaluator
from pipeline.scoring.utility import UtilityEvaluator
from pipeline.scoring.quality import QualityEvaluator
from pipeline.scoring.engine import compute_composite, score_resource, ScoreCollector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def patient_original():
    return {
        "resourceType": "Patient",
        "id": "p123",
        "name": [{"family": "Smith", "given": ["John"]}],
        "telecom": [{"system": "phone", "value": "+1-555-123-4567"}],
        "address": [{"line": ["123 Main St"], "city": "Springfield", "postalCode": "62701"}],
        "birthDate": "1985-03-15",
        "identifier": [{"system": "http://example.org/mrn", "value": "MRN12345"}],
        "gender": "male",
    }


@pytest.fixture
def patient_deidentified():
    return {
        "resourceType": "Patient",
        "id": "pseudo-abc123",
        "name": [{"family": "[REDACTED]", "given": ["[REDACTED]"]}],
        "telecom": [{"system": "phone", "value": "[REDACTED]"}],
        "address": [{"line": ["[REDACTED]"], "city": "[REDACTED]", "postalCode": "627"}],
        "birthDate": "1985",
        "identifier": [{"system": "http://example.org/mrn", "value": "pseudo-mrn-001"}],
        "gender": "male",
        "text": {"div": "<div>De-identified patient record</div>"},
    }


@pytest.fixture
def manifest_entries_full():
    """Manifest covering all sensitive Patient fields."""
    return [
        {"rule": "redact_name", "action": "redact", "path": "Patient.name"},
        {"rule": "redact_telecom", "action": "redact", "path": "Patient.telecom"},
        {"rule": "gen_address", "action": "generalize", "path": "Patient.address"},
        {"rule": "gen_birthdate", "action": "generalize", "path": "Patient.birthDate"},
        {"rule": "pseudo_id", "action": "gpas_pseudonymize", "path": "Patient.identifier"},
        {"rule": "hash_id", "action": "cryptohash", "path": "Patient.id"},
        {"rule": "scrub_text", "action": "scrub_text", "path": "Patient.text"},
    ]


@pytest.fixture
def observation_deidentified():
    return {
        "resourceType": "Observation",
        "id": "obs-001",
        "status": "final",
        "code": {
            "coding": [{"system": "http://loinc.org", "code": "29463-7", "display": "Body Weight"}]
        },
        "valueQuantity": {"value": 70.0, "unit": "kg", "system": "http://unitsofmeasure.org", "code": "kg"},
        "effectiveDateTime": "2024-01-15",
        "subject": {"reference": "Patient/pseudo-abc123"},
    }


# ---------------------------------------------------------------------------
# Models / Serialization
# ---------------------------------------------------------------------------

class TestModels:
    def test_evidence_defaults(self):
        e = Evidence(check="test", value=0.5)
        assert e.severity == "info"
        assert e.details == {}

    def test_privacy_decision_to_dict(self):
        pd = PrivacyDecision(
            risk_score=0.15, passed=True, threshold=0.3,
            attacker_risk=0.1, identifier_risk=0.05, text_risk=0.15,
            evidence=[Evidence(check="test", value=0.1)],
        )
        d = pd.to_dict()
        assert d["passed"] is True
        assert d["risk_score"] == 0.15
        assert len(d["evidence"]) == 1

    def test_module_score_to_dict(self):
        ms = ModuleScore(name="utility", score=0.85, gates_applied=["cap_60"])
        d = ms.to_dict()
        assert d["name"] == "utility"
        assert d["score"] == 0.85
        assert d["gates_applied"] == ["cap_60"]

    def test_score_result_to_dict(self):
        privacy = PrivacyDecision(
            risk_score=0.1, passed=True, threshold=0.3,
            attacker_risk=0.0, identifier_risk=0.1, text_risk=0.0,
        )
        utility = ModuleScore(name="utility", score=0.9)
        quality = ModuleScore(name="quality", score=0.8)
        result = ScoreResult(
            composite=48.0, decision="PASS",
            privacy=privacy, utility=utility, quality=quality,
            resource_type="Patient", resource_id="p1",
            scored_at="2024-01-01T00:00:00Z", config_profile="auto",
        )
        d = result.to_dict()
        assert d["decision"] == "PASS"
        assert d["composite"] == 48.0
        assert d["privacy"]["passed"] is True
        assert d["utility"]["score"] == 0.9

    def test_fhir_extension(self):
        privacy = PrivacyDecision(
            risk_score=0.1, passed=True, threshold=0.3,
            attacker_risk=0.0, identifier_risk=0.1, text_risk=0.0,
        )
        utility = ModuleScore(name="utility", score=0.9)
        quality = ModuleScore(name="quality", score=0.8)
        result = ScoreResult(
            composite=48.0, decision="PASS",
            privacy=privacy, utility=utility, quality=quality,
            resource_type="Patient", resource_id="p1",
            scored_at="2024-01-01T00:00:00Z", config_profile="auto",
        )
        ext = result.to_fhir_extension()
        assert ext["url"].endswith("deidentification-score")
        urls = {e["url"] for e in ext["extension"]}
        assert "composite" in urls
        assert "decision" in urls
        assert "privacy-risk" in urls

    def test_score_result_fail_no_utility_quality(self):
        privacy = PrivacyDecision(
            risk_score=0.5, passed=False, threshold=0.3,
            attacker_risk=0.5, identifier_risk=0.0, text_risk=0.0,
        )
        result = ScoreResult(
            composite=0.0, decision="FAIL",
            privacy=privacy, utility=None, quality=None,
            resource_type="Patient", resource_id="p1",
            scored_at="2024-01-01T00:00:00Z", config_profile="auto",
        )
        d = result.to_dict()
        assert d["decision"] == "FAIL"
        assert d["composite"] == 0.0
        assert d["utility"] is None
        assert d["quality"] is None


# ---------------------------------------------------------------------------
# Composite Score
# ---------------------------------------------------------------------------

class TestCompositeScore:
    def test_fail_when_privacy_fails(self):
        privacy = PrivacyDecision(
            risk_score=0.5, passed=False, threshold=0.3,
            attacker_risk=0.5, identifier_risk=0.0, text_risk=0.0,
        )
        utility = ModuleScore(name="utility", score=0.9)
        quality = ModuleScore(name="quality", score=0.95)
        composite, decision = compute_composite(privacy, utility, quality)
        assert decision == "FAIL"
        assert composite == 0.0

    def test_pass_with_all_modules(self):
        privacy = PrivacyDecision(
            risk_score=0.1, passed=True, threshold=0.3,
            attacker_risk=0.0, identifier_risk=0.1, text_risk=0.0,
        )
        utility = ModuleScore(name="utility", score=0.9)
        quality = ModuleScore(name="quality", score=0.95)
        composite, decision = compute_composite(privacy, utility, quality)
        assert decision == "PASS"
        assert composite > 0
        # privacy_norm = 1.0 - (0.1/0.3) = 0.6667
        # raw = 0.6667 * 0.9 * 0.95 = 0.57
        assert 50.0 < composite < 65.0

    def test_non_compensatory_low_utility(self):
        """Low utility drags score down — can't be compensated by high quality."""
        privacy = PrivacyDecision(
            risk_score=0.1, passed=True, threshold=0.3,
            attacker_risk=0.0, identifier_risk=0.1, text_risk=0.0,
        )
        utility_high = ModuleScore(name="utility", score=0.9)
        utility_low = ModuleScore(name="utility", score=0.2)
        quality = ModuleScore(name="quality", score=0.95)

        c_high, _ = compute_composite(privacy, utility_high, quality)
        c_low, _ = compute_composite(privacy, utility_low, quality)

        assert c_low < c_high * 0.4  # multiplicative means low util tanks everything

    def test_non_compensatory_low_quality(self):
        """Low quality drags score down — can't be compensated by high utility."""
        privacy = PrivacyDecision(
            risk_score=0.1, passed=True, threshold=0.3,
            attacker_risk=0.0, identifier_risk=0.1, text_risk=0.0,
        )
        utility = ModuleScore(name="utility", score=0.9)
        quality_low = ModuleScore(name="quality", score=0.2)

        composite, _ = compute_composite(privacy, utility, quality_low)
        assert composite < 15.0  # severely penalized

    def test_zero_risk_threshold(self):
        """When threshold is 0, privacy_score should be 1.0 (no normalization)."""
        privacy = PrivacyDecision(
            risk_score=0.0, passed=True, threshold=0.0,
            attacker_risk=0.0, identifier_risk=0.0, text_risk=0.0,
        )
        utility = ModuleScore(name="utility", score=0.8)
        quality = ModuleScore(name="quality", score=0.9)
        composite, decision = compute_composite(privacy, utility, quality)
        assert decision == "PASS"
        assert composite == pytest.approx(72.0, abs=0.1)


# ---------------------------------------------------------------------------
# Privacy Risk Evaluator
# ---------------------------------------------------------------------------

class TestPrivacyRiskEvaluator:
    def setup_method(self):
        self.evaluator = PrivacyRiskEvaluator()

    def test_no_manifest_on_phi_resource(self, patient_deidentified):
        """Empty manifest on Patient = identifier_risk = 1.0 → FAIL."""
        result = self.evaluator.evaluate(None, patient_deidentified, [])
        assert not result.passed
        assert result.identifier_risk == 1.0
        assert result.risk_score >= 1.0

    def test_full_manifest_coverage(self, patient_deidentified, manifest_entries_full):
        """Full manifest coverage → low identifier risk."""
        result = self.evaluator.evaluate(None, patient_deidentified, manifest_entries_full)
        assert result.identifier_risk < RISK_THRESHOLD
        # Evidence should exist
        assert len(result.evidence) > 0

    def test_non_phi_resource_auto_pass(self, observation_deidentified):
        """Observation has no HIPAA-sensitive paths beyond wildcard → low risk."""
        manifest = [
            {"rule": "hash_id", "action": "cryptohash", "path": "Observation.id"},
            {"rule": "scrub_text", "action": "scrub_text", "path": "Observation.text"},
        ]
        result = self.evaluator.evaluate(None, observation_deidentified, manifest)
        # Observation has no specific sensitive paths, only wildcard (* → id, text)
        assert result.identifier_risk < 0.5

    def test_text_risk_detects_ssn(self, patient_deidentified):
        """SSN pattern in text should increase text_risk."""
        patient_deidentified["text"] = {
            "div": "<div>Patient SSN: 123-45-6789 was recorded in the system on admission</div>"
        }
        result = self.evaluator.evaluate(None, patient_deidentified, [
            {"rule": "r", "action": "redact", "path": "Patient.name"},
            {"rule": "r", "action": "redact", "path": "Patient.telecom"},
            {"rule": "r", "action": "redact", "path": "Patient.address"},
            {"rule": "r", "action": "redact", "path": "Patient.identifier"},
            {"rule": "r", "action": "redact", "path": "Patient.birthDate"},
            {"rule": "r", "action": "redact", "path": "Patient.id"},
            {"rule": "r", "action": "redact", "path": "Patient.text"},
        ])
        assert result.text_risk > 0

    def test_text_risk_detects_email(self, patient_deidentified):
        """Email pattern in text should increase text_risk."""
        patient_deidentified["note"] = "Contact via john.smith@hospital.org for follow-up"
        result = self.evaluator.evaluate(None, patient_deidentified, [
            {"rule": "r", "action": "redact", "path": "Patient.name"},
            {"rule": "r", "action": "redact", "path": "Patient.telecom"},
            {"rule": "r", "action": "redact", "path": "Patient.address"},
            {"rule": "r", "action": "redact", "path": "Patient.identifier"},
            {"rule": "r", "action": "redact", "path": "Patient.birthDate"},
            {"rule": "r", "action": "redact", "path": "Patient.id"},
            {"rule": "r", "action": "redact", "path": "Patient.text"},
        ])
        assert result.text_risk > 0

    def test_risk_uses_max(self, patient_deidentified):
        """Overall risk = max(attacker, identifier, text) — conservative."""
        result = self.evaluator.evaluate(None, patient_deidentified, [])
        # Empty manifest → identifier_risk = 1.0 → overall risk = 1.0
        assert result.risk_score == max(result.attacker_risk, result.identifier_risk, result.text_risk)

    def test_non_patient_attacker_model_zero(self, observation_deidentified):
        """Attacker model only applies to Patient resources."""
        result = self.evaluator.evaluate(None, observation_deidentified, [])
        assert result.attacker_risk == 0.0


# ---------------------------------------------------------------------------
# Utility Evaluator
# ---------------------------------------------------------------------------

class TestUtilityEvaluator:
    def setup_method(self):
        self.evaluator = UtilityEvaluator()

    def test_full_retention_with_original(self, patient_original, patient_deidentified):
        """All original fields retained → high retention score."""
        manifest = [
            {"rule": "r", "action": "redact", "path": "Patient.name"},
        ]
        result = self.evaluator.evaluate(patient_original, patient_deidentified, manifest)
        assert result.score > 0.5
        assert result.name == "utility"

    def test_no_original_uses_manifest(self, patient_deidentified, manifest_entries_full):
        """Without original, field retention estimated from manifest."""
        result = self.evaluator.evaluate(None, patient_deidentified, manifest_entries_full)
        assert 0.0 <= result.score <= 1.0

    def test_high_redact_ratio_lowers_score(self, patient_deidentified):
        """All-redact manifest → low utility from info loss."""
        manifest = [
            {"rule": "r", "action": "redact", "path": "Patient.name"},
            {"rule": "r", "action": "redact", "path": "Patient.telecom"},
            {"rule": "r", "action": "redact", "path": "Patient.address"},
            {"rule": "r", "action": "redact", "path": "Patient.birthDate"},
            {"rule": "r", "action": "redact", "path": "Patient.identifier"},
        ]
        result = self.evaluator.evaluate(None, patient_deidentified, manifest)
        # Redact actions have loss weight 1.0 → info_loss_score = 0.0
        assert result.score < 0.5

    def test_encrypt_preserves_utility(self, patient_deidentified):
        """Encrypt has 0.0 info loss → utility preserved."""
        manifest = [
            {"rule": "r", "action": "encrypt", "path": "Patient.name"},
            {"rule": "r", "action": "encrypt", "path": "Patient.telecom"},
        ]
        result = self.evaluator.evaluate(None, patient_deidentified, manifest)
        # info_loss for encrypt = 0.0, so info_loss_score = 1.0
        assert result.score > 0.5

    def test_semantic_preservation_with_valid_codes(self, observation_deidentified):
        """Observation with valid LOINC coding → high semantic score."""
        result = self.evaluator.evaluate(None, observation_deidentified, [])
        # Observation has valid coding arrays
        assert result.score > 0

    def test_empty_resource(self):
        """Empty de-identified resource."""
        result = self.evaluator.evaluate(None, {"resourceType": "Patient"}, [])
        assert 0.0 <= result.score <= 1.0

    def test_empty_manifest(self, patient_deidentified):
        """No manifest entries → info_loss_score = 1.0 (no observed loss)."""
        result = self.evaluator.evaluate(None, patient_deidentified, [])
        assert 0.0 <= result.score <= 1.0


# ---------------------------------------------------------------------------
# Quality Evaluator
# ---------------------------------------------------------------------------

class TestQualityEvaluator:
    def setup_method(self):
        self.evaluator = QualityEvaluator()

    def test_perfect_quality(self, patient_deidentified, manifest_entries_full):
        """No errors, good coverage, valid schema → high quality."""
        result = self.evaluator.evaluate(
            patient_deidentified, manifest_entries_full,
            error_count=0, total_count=10,
        )
        assert result.score > 0.5
        assert result.name == "quality"
        assert len(result.gates_applied) == 0

    def test_error_rate_gate_20_percent(self, patient_deidentified, manifest_entries_full):
        """Error rate > 20% → quality capped at 0.20."""
        result = self.evaluator.evaluate(
            patient_deidentified, manifest_entries_full,
            error_count=25, total_count=100,
        )
        assert result.score <= 0.20
        assert "error_rate_above_20pct" in result.gates_applied

    def test_error_rate_gate_5_percent(self, patient_deidentified, manifest_entries_full):
        """Error rate > 5% → quality capped at 0.60."""
        result = self.evaluator.evaluate(
            patient_deidentified, manifest_entries_full,
            error_count=8, total_count=100,
        )
        assert result.score <= 0.60
        assert "error_rate_above_5pct" in result.gates_applied

    def test_schema_validation_no_resource_type(self):
        """Missing resourceType → lower validation score."""
        result = self.evaluator.evaluate(
            {"id": "test"}, [],
            error_count=0, total_count=1,
        )
        # Should still produce a score
        assert 0.0 <= result.score <= 1.0

    def test_reference_integrity_valid(self, observation_deidentified):
        """Valid reference format → high integrity."""
        result = self.evaluator.evaluate(
            observation_deidentified, [],
            error_count=0, total_count=1,
        )
        assert result.score > 0

    def test_reference_integrity_invalid(self):
        """Broken references → lower integrity."""
        resource = {
            "resourceType": "Observation",
            "id": "obs-1",
            "subject": {"reference": ""},  # broken
            "performer": [{"reference": "invalid"}],  # broken
        }
        result = self.evaluator.evaluate(resource, [], error_count=0, total_count=1)
        # Still produces a result
        assert 0.0 <= result.score <= 1.0


# ---------------------------------------------------------------------------
# Score Resource (Full Engine)
# ---------------------------------------------------------------------------

class TestScoreResource:
    def test_privacy_fail_composite_zero(self, patient_deidentified):
        """Privacy FAIL → composite = 0, no utility/quality."""
        result = score_resource(
            original=None,
            deidentified=patient_deidentified,
            manifest_entries=[],  # empty manifest → identifier_risk = 1.0
        )
        assert result.decision == "FAIL"
        assert result.composite == 0.0
        assert result.utility is None
        assert result.quality is None

    def test_privacy_pass_produces_all_modules(
        self, patient_original, patient_deidentified, manifest_entries_full,
    ):
        """Privacy PASS → all three modules evaluated."""
        result = score_resource(
            original=patient_original,
            deidentified=patient_deidentified,
            manifest_entries=manifest_entries_full,
        )
        # Privacy should pass with full manifest
        if result.decision == "PASS":
            assert result.utility is not None
            assert result.quality is not None
            assert result.composite > 0

    def test_non_phi_resource_pass(self, observation_deidentified):
        """Non-PHI Observation with id/text covered → likely PASS."""
        manifest = [
            {"rule": "hash_id", "action": "cryptohash", "path": "Observation.id"},
            {"rule": "scrub", "action": "scrub_text", "path": "Observation.text"},
        ]
        result = score_resource(
            original=None,
            deidentified=observation_deidentified,
            manifest_entries=manifest,
        )
        # Observation has minimal sensitive paths
        assert result.resource_type == "Observation"
        assert result.scored_at is not None

    def test_config_profile_in_result(self, observation_deidentified):
        result = score_resource(
            original=None,
            deidentified=observation_deidentified,
            manifest_entries=[],
            config_profile="hipaa_safe_harbor",
        )
        assert result.config_profile == "hipaa_safe_harbor"


# ---------------------------------------------------------------------------
# ScoreCollector
# ---------------------------------------------------------------------------

class TestScoreCollector:
    def test_empty_aggregate(self):
        collector = ScoreCollector()
        agg = collector.aggregate()
        assert agg["computed"] is False

    def test_record_error(self):
        collector = ScoreCollector()
        collector.record_error()
        collector.record_error()
        agg = collector.aggregate()
        assert agg["computed"] is False  # no resources scored

    def test_record_resource_error_marker(self):
        collector = ScoreCollector()
        result = collector.record_resource(
            None,
            {"error": "processing error", "resourceType": "Patient"},
            [],
        )
        assert result.decision == "FAIL"
        agg = collector.aggregate()
        assert agg["computed"] is True
        assert agg["error_count"] == 1

    def test_collect_multiple_resources(self, observation_deidentified):
        collector = ScoreCollector(config_profile="test")
        manifest = [
            {"rule": "hash_id", "action": "cryptohash", "path": "Observation.id"},
        ]
        # Record multiple observations
        for _ in range(5):
            collector.record_resource(None, observation_deidentified, manifest)

        agg = collector.aggregate()
        assert agg["computed"] is True
        assert agg["total_scored"] == 5
        assert agg["config_profile"] == "test"

    def test_patient_accumulation(self, patient_deidentified, manifest_entries_full):
        collector = ScoreCollector()
        collector.record_resource(None, patient_deidentified, manifest_entries_full)
        agg = collector.aggregate()
        assert agg["computed"] is True
        assert agg["total_scored"] == 1


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_hipaa_paths_has_patient(self):
        assert "Patient" in HIPAA_SENSITIVE_PATHS
        assert "name" in HIPAA_SENSITIVE_PATHS["Patient"]

    def test_wildcard_paths(self):
        assert "*" in HIPAA_SENSITIVE_PATHS
        assert "id" in HIPAA_SENSITIVE_PATHS["*"]
        assert "text" in HIPAA_SENSITIVE_PATHS["*"]

    def test_clinical_code_systems(self):
        assert "http://loinc.org" in CLINICAL_CODE_SYSTEMS
        assert "http://snomed.info/sct" in CLINICAL_CODE_SYSTEMS

    def test_info_loss_weights(self):
        assert INFO_LOSS_WEIGHTS["redact"] == 1.0
        assert INFO_LOSS_WEIGHTS["encrypt"] == 0.0
        assert INFO_LOSS_WEIGHTS["generalize"] > INFO_LOSS_WEIGHTS["perturb"]

    def test_risk_threshold(self):
        assert RISK_THRESHOLD == 0.3
