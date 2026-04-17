"""Scoring engine — orchestrates privacy gate + utility + quality.

Implements the constraint-based scoring model:
1. Privacy risk is evaluated first as a hard constraint (PASS/FAIL)
2. If privacy FAILs → composite = 0, utility and quality are NOT evaluated
3. If privacy PASSes → composite = privacy_norm × utility × quality (multiplicative)
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

from pipeline.scoring.models import Evidence, ModuleScore, PrivacyDecision, ScoreResult
from pipeline.scoring.constants import RISK_THRESHOLD
from pipeline.scoring.privacy import PrivacyRiskEvaluator
from pipeline.scoring.utility import UtilityEvaluator
from pipeline.scoring.quality import QualityEvaluator

_log = logging.getLogger(__name__)
_audit = logging.getLogger("medanon.audit")

try:
    from utils.metrics import (
        SCORE_COMPOSITE,
        SCORE_PRIVACY_RISK,
        SCORE_DECISIONS,
        SCORE_DURATION,
    )

    _HAS_METRICS = True
except ImportError:
    _HAS_METRICS = False

_privacy_eval = PrivacyRiskEvaluator()
_utility_eval = UtilityEvaluator()
_quality_eval = QualityEvaluator()

# Maximum number of Patient resources retained for batch-level k-anonymity.
# Exports with more patients use reservoir sampling to keep a representative
# subsample, bounding memory to ~_MAX_PATIENTS × avg_patient_size.
_MAX_PATIENTS: int = int(os.environ.get("MEDANON_SCORE_MAX_PATIENTS", "10000"))


def compute_composite(
    privacy: PrivacyDecision,
    utility: ModuleScore,
    quality: ModuleScore,
) -> tuple[float, str]:
    """Multiplicative aggregation — no dimension compensates for another."""
    if not privacy.passed:
        return 0.0, "FAIL"
    if privacy.threshold <= 0:
        privacy_score = 1.0
    else:
        privacy_score = 1.0 - (privacy.risk_score / privacy.threshold)
    raw = privacy_score * utility.score * quality.score
    composite = round(raw * 100, 1)
    # A composite of exactly 0.0 means at least one dimension is zero (e.g.
    # utility or quality is completely absent) — return FAIL even when privacy
    # passes, since a score of zero is not a meaningful pass.
    decision = "PASS" if composite > 0.0 else "FAIL"
    return composite, decision


def score_resource(
    original: dict | None,
    deidentified: dict,
    manifest_entries: list[dict],
    settings: Any = None,
    config_profile: str = "auto",
    error_count: int = 0,
    total_count: int = 1,
) -> ScoreResult:
    """Score a single de-identified resource."""
    t0 = time.monotonic()

    privacy = _privacy_eval.evaluate(original, deidentified, manifest_entries, settings)

    utility: ModuleScore | None = None
    quality: ModuleScore | None = None
    composite = 0.0
    decision = "FAIL"

    if privacy.passed:
        utility = _utility_eval.evaluate(original, deidentified, manifest_entries)
        quality = _quality_eval.evaluate(
            deidentified,
            manifest_entries,
            error_count,
            total_count,
            settings,
        )
        composite, decision = compute_composite(privacy, utility, quality)

    result = ScoreResult(
        composite=composite,
        decision=decision,
        privacy=privacy,
        utility=utility,
        quality=quality,
        resource_type=deidentified.get("resourceType", "Unknown"),
        resource_id=deidentified.get("id"),
        scored_at=ScoreResult.now_iso(),
        config_profile=config_profile,
    )

    elapsed = time.monotonic() - t0
    _log.debug(
        "scored resource_type=%s decision=%s composite=%.1f in %.3fs",
        result.resource_type,
        result.decision,
        result.composite,
        elapsed,
    )

    if _HAS_METRICS:
        SCORE_COMPOSITE.labels(
            resource_type=result.resource_type, decision=result.decision
        ).observe(result.composite)
        SCORE_PRIVACY_RISK.labels(resource_type=result.resource_type).observe(
            privacy.risk_score
        )
        SCORE_DECISIONS.labels(decision=result.decision).inc()
        SCORE_DURATION.labels(resource_type=result.resource_type).observe(elapsed)

    return result


class ScoreCollector:
    """Accumulates per-resource scores during job execution.

    Follows the same pattern as ``JobSummaryCollector`` — call
    ``record_resource()`` for each processed resource, then ``aggregate()``
    at job completion for the batch-level k-anonymity evaluation.
    """

    __slots__ = (
        "_pass_count",
        "_fail_count",
        "_composite_sum",
        "_min_composite",
        "_utility_sum",
        "_quality_sum",
        "_patient_qis",
        "_patient_manifests",
        "_patient_seen",
        "_error_count",
        "_total_count",
        "_config_profile",
    )

    def __init__(self, config_profile: str = "auto") -> None:
        self._pass_count: int = 0
        self._fail_count: int = 0
        self._composite_sum: float = 0.0
        self._min_composite: float = float("inf")
        self._utility_sum: float = 0.0
        self._quality_sum: float = 0.0
        self._patient_qis: list[tuple[str, str, str]] = []
        self._patient_manifests: list[list[dict]] = []
        self._patient_seen: int = 0
        self._error_count: int = 0
        self._total_count: int = 0
        self._config_profile = config_profile

    def record_resource(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        settings: Any = None,
    ) -> ScoreResult:
        """Score one resource and accumulate running totals."""
        self._total_count += 1
        if "error" in deidentified:
            self._error_count += 1
            self._fail_count += 1
            # Create a minimal FAIL result for error resources
            result = ScoreResult(
                composite=0.0,
                decision="FAIL",
                privacy=PrivacyDecision(
                    risk_score=1.0,
                    passed=False,
                    threshold=RISK_THRESHOLD,
                    attacker_risk=0.0,
                    identifier_risk=1.0,
                    text_risk=0.0,
                    evidence=[
                        Evidence(
                            check="processing_error",
                            value=1.0,
                            details={"error": deidentified.get("error", "unknown")},
                            severity="critical",
                        )
                    ],
                ),
                utility=None,
                quality=None,
                resource_type=deidentified.get("resourceType", "Unknown"),
                resource_id=None,
                scored_at=ScoreResult.now_iso(),
                config_profile=self._config_profile,
            )
            self._composite_sum += 0.0
            self._min_composite = min(self._min_composite, 0.0)
            return result

        result = score_resource(
            original,
            deidentified,
            manifest_entries,
            settings,
            self._config_profile,
        )

        # Accumulate running totals
        self._composite_sum += result.composite
        self._min_composite = min(self._min_composite, result.composite)
        if result.decision == "PASS":
            self._pass_count += 1
            if result.utility:
                self._utility_sum += result.utility.score
            if result.quality:
                self._quality_sum += result.quality.score
        else:
            self._fail_count += 1

        # Accumulate Patient QI tuples for batch-level k-anonymity (reservoir sampling).
        # Only the 3 quasi-identifier fields are retained — not the full dict —
        # to bound memory usage (~100 bytes/patient instead of ~5 KB).
        if deidentified.get("resourceType") == "Patient":
            self._patient_seen += 1
            try:
                from medanon_core.analytics.risk import _extract_patient_qi
                qi = _extract_patient_qi(deidentified)
            except ImportError:
                qi = ("", "", "")
            if len(self._patient_qis) < _MAX_PATIENTS:
                self._patient_qis.append(qi)
                self._patient_manifests.append(manifest_entries)
            else:
                # Reservoir sampling: replace a random element with probability
                # _MAX_PATIENTS / _patient_seen to maintain a uniform subsample.
                j = random.randrange(self._patient_seen)
                if j < _MAX_PATIENTS:
                    self._patient_qis[j] = qi
                    self._patient_manifests[j] = manifest_entries

        return result

    def record_error(self) -> None:
        self._error_count += 1
        self._total_count += 1

    def aggregate(self) -> dict:
        """Produce batch-level aggregate score with full k-anonymity."""
        total = self._pass_count + self._fail_count
        if total == 0:
            return {"computed": False, "reason": "no resources scored"}

        # Batch-level privacy with full k-anonymity (from pre-extracted QI tuples)
        batch_privacy: PrivacyDecision | None = None
        if self._patient_qis:
            batch_privacy = _privacy_eval.evaluate_batch_from_qis(
                self._patient_qis,
                self._patient_manifests,
            )

        avg_composite = self._composite_sum / total if total else 0.0
        min_composite = (
            self._min_composite if self._min_composite != float("inf") else 0.0
        )

        # Per-module averages (only from PASS resources)
        avg_utility = self._utility_sum / max(self._pass_count, 1)
        avg_quality = self._quality_sum / max(self._pass_count, 1)

        return {
            "computed": True,
            "total_scored": total,
            "pass_count": self._pass_count,
            "fail_count": self._fail_count,
            "error_count": self._error_count,
            "avg_composite": round(avg_composite, 1),
            "min_composite": round(min_composite, 1),
            "avg_utility": round(avg_utility, 4),
            "avg_quality": round(avg_quality, 4),
            "batch_privacy": batch_privacy.to_dict() if batch_privacy else None,
            "config_profile": self._config_profile,
        }
