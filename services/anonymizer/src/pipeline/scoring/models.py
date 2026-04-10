"""Scoring engine data models.

Dataclasses for the constraint-based scoring engine output.
Privacy produces a PrivacyDecision (pass/fail gate), utility and quality
produce ModuleScore values, and ScoreResult combines everything.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

SCORE_EXTENSION_URL = "https://medanon.local/StructureDefinition/deidentification-score"


@dataclass(slots=True)
class Evidence:
    """Single piece of scoring evidence."""

    check: str
    value: float
    details: dict = field(default_factory=dict)
    severity: str = "info"  # "info" | "warning" | "critical"


@dataclass(slots=True)
class PrivacyDecision:
    """Result of the privacy risk evaluation (hard constraint)."""

    risk_score: float
    passed: bool
    threshold: float
    attacker_risk: float
    identifier_risk: float
    text_risk: float
    evidence: list[Evidence] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "risk_score": round(self.risk_score, 4),
            "passed": self.passed,
            "threshold": self.threshold,
            "attacker_risk": round(self.attacker_risk, 4),
            "identifier_risk": round(self.identifier_risk, 4),
            "text_risk": round(self.text_risk, 4),
            "evidence": [
                {
                    "check": e.check,
                    "value": round(e.value, 4),
                    "details": e.details,
                    "severity": e.severity,
                }
                for e in self.evidence
            ],
        }


@dataclass(slots=True)
class ModuleScore:
    """Result of a continuous scoring module (utility or quality)."""

    name: str
    score: float
    evidence: list[Evidence] = field(default_factory=list)
    gates_applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "evidence": [
                {
                    "check": e.check,
                    "value": round(e.value, 4),
                    "details": e.details,
                    "severity": e.severity,
                }
                for e in self.evidence
            ],
            "gates_applied": self.gates_applied,
        }


@dataclass(slots=True)
class ScoreResult:
    """Complete scoring output for one resource or an aggregated batch."""

    composite: float
    decision: str  # "PASS" | "FAIL"
    privacy: PrivacyDecision
    utility: ModuleScore | None
    quality: ModuleScore | None
    resource_type: str
    resource_id: str | None
    scored_at: str
    config_profile: str

    def to_dict(self) -> dict:
        return {
            "composite": round(self.composite, 1),
            "decision": self.decision,
            "privacy": self.privacy.to_dict(),
            "utility": self.utility.to_dict() if self.utility else None,
            "quality": self.quality.to_dict() if self.quality else None,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "scored_at": self.scored_at,
            "config_profile": self.config_profile,
        }

    def to_fhir_extension(self) -> dict:
        """Produce a FHIR-compatible extension dict (no PHI)."""
        exts = [
            {"url": "composite", "valueDecimal": round(self.composite, 1)},
            {"url": "decision", "valueCode": self.decision},
            {"url": "privacy-risk", "valueDecimal": round(self.privacy.risk_score, 4)},
        ]
        if self.utility is not None:
            exts.append(
                {"url": "utility", "valueDecimal": round(self.utility.score, 4)}
            )
        if self.quality is not None:
            exts.append(
                {"url": "quality", "valueDecimal": round(self.quality.score, 4)}
            )
        exts.append({"url": "scored-at", "valueDateTime": self.scored_at})
        return {"url": SCORE_EXTENSION_URL, "extension": exts}

    @staticmethod
    def now_iso() -> str:
        return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
