"""Scoring engine  orchestrates privacy gate + utility + quality.

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

from scoring.models import Evidence, ModuleScore, PrivacyDecision, ScoreResult
from scoring.constants import RISK_THRESHOLD
from scoring.privacy import PrivacyRiskEvaluator
from scoring.utility import UtilityEvaluator
from scoring.quality import QualityEvaluator
from scoring._metrics import (
    SCORE_COMPOSITE,
    SCORE_DECISIONS,
    SCORE_DURATION,
    SCORE_PRIVACY_RISK,
    _HAS_METRICS,
)

_log = logging.getLogger(__name__)
_audit = logging.getLogger("medanon.audit")

_privacy_eval = PrivacyRiskEvaluator()
_utility_eval = UtilityEvaluator()
_quality_eval = QualityEvaluator()

# Maximum number of Patient resources retained for batch-level k-anonymity.
# Exports with more patients use reservoir sampling to keep a representative
# subsample, bounding memory to ~_MAX_PATIENTS × avg_patient_size.
_MAX_PATIENTS: int = int(os.environ.get("MEDANON_SCORE_MAX_PATIENTS", "10000"))


# Quasi-identifier extractor, resolved once and cached at module level.  It was
# previously imported inside ``record_resource`` on *every* Patient (inside the
# accumulator lock)  hoisting the resolution out of the per-resource hot loop
# avoids a repeated import lookup in the critical section.
_extract_patient_qi = None


def _get_qi_extractor():
    """Return the cached ``analytics.risk._extract_patient_qi`` (or a no-op)."""
    global _extract_patient_qi
    if _extract_patient_qi is None:
        try:
            from analytics.risk import _extract_patient_qi as _fn

            _extract_patient_qi = _fn
        except ImportError:
            _extract_patient_qi = lambda _r: ("", "", "")  # noqa: E731 - tiny fallback
    return _extract_patient_qi


def compute_composite(
    privacy: PrivacyDecision,
    utility: ModuleScore,
    quality: ModuleScore,
) -> tuple[float, str]:
    """Multiplicative aggregation  no dimension compensates for another."""
    if not privacy.passed:
        return 0.0, "FAIL"
    # Privacy contribution = residual-privacy level = 1 - re-identification risk.
    # ``risk_score`` and this factor are both dimensionless in [0, 1]; the gate
    # threshold is a PASS/FAIL decision boundary, not a normaliser, so it must
    # not scale the composite. The previous ``1 - risk/threshold`` mapping drove
    # any resource that merely *passed* near the boundary toward 0  a clean
    # privacy posture could still score ~0  which is why legitimately safe
    # cohorts graded F. A passed resource now contributes in proportion to its
    # actual residual risk.
    privacy_score = max(0.0, min(1.0, 1.0 - privacy.risk_score))
    raw = privacy_score * utility.score * quality.score
    # `composite` is on the 0-100 scale (already multiplied by 100). Persist it
    # as-is in `score.avg_composite`; the React UI does Math.round(value) + "%"
    #  do NOT multiply by 100 again at the display layer.
    composite = round(raw * 100, 1)
    # A composite of exactly 0.0 means utility or quality is completely absent
    # return FAIL since a zero score is not a meaningful pass.
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
    """Score a single de-identified resource.

    When ``SCORING_SERVICE_URL`` is set the call is delegated to the scoring
    microservice. Any transport/circuit-breaker failure falls back to the
    in-process engine so privacy assessments never silently disappear.
    """
    remote = _get_remote_client()
    if remote is not None:
        try:
            return remote.score(
                original=original,
                deidentified=deidentified,
                manifest_entries=manifest_entries,
                config_profile=config_profile,
                error_count=error_count,
                total_count=total_count,
            )
        except Exception as exc:  # noqa: BLE001  degrade gracefully
            _log.warning("remote scoring failed, falling back to local: %s", exc)

    return _score_resource_local(
        original,
        deidentified,
        manifest_entries,
        settings,
        config_profile,
        error_count,
        total_count,
    )


_REMOTE_CLIENT = None
_REMOTE_CLIENT_URL = ""
_remote_client_provider = None


def set_remote_client_provider(provider) -> None:
    """Inject the remote scoring-client factory (composition root).

    The anonymizer wires ``integrations.scoring.get_remote_scoring_client`` here
    at startup so that, when ``SCORING_SERVICE_URL`` is set, scoring delegates to
    the microservice. When no provider is injected (e.g. the scoring microservice
    itself, which is the local engine) scoring is always in-process.
    """
    global _remote_client_provider
    _remote_client_provider = provider


def _get_remote_client():
    """Return the injected remote scoring client when configured, else None."""
    global _REMOTE_CLIENT, _REMOTE_CLIENT_URL
    # Common/local path (no provider injected, e.g. the scoring microservice or
    # scoring-locally): bail before the per-call env read.
    if _remote_client_provider is None:
        return None
    url = os.environ.get("SCORING_SERVICE_URL", "").strip()
    if not url:
        _REMOTE_CLIENT = None
        _REMOTE_CLIENT_URL = ""
        return None
    if _REMOTE_CLIENT is None or _REMOTE_CLIENT_URL != url:
        _REMOTE_CLIENT = _remote_client_provider()
        _REMOTE_CLIENT_URL = url
    return _REMOTE_CLIENT


def _score_resource_local(
    original: dict | None,
    deidentified: dict,
    manifest_entries: list[dict],
    settings: Any = None,
    config_profile: str = "auto",
    error_count: int = 0,
    total_count: int = 1,
) -> ScoreResult:
    """In-process implementation. Always available regardless of env config."""
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

    Follows the same pattern as ``JobSummaryCollector``  call
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
        "_patient_seen",
        "_error_count",
        "_total_count",
        "_config_profile",
        "_text_risk_hits",
        "_identifier_risk_hits",
        "_config_risk_sum",
        "_config_risk_count",
        "_uncovered_paths",
        "_lock",
    )

    def __init__(self, config_profile: str = "auto") -> None:
        import threading

        self._pass_count: int = 0
        self._fail_count: int = 0
        self._composite_sum: float = 0.0
        self._min_composite: float = float("inf")
        self._utility_sum: float = 0.0
        self._quality_sum: float = 0.0
        self._patient_qis: list[tuple[str, str, str]] = []
        self._patient_seen: int = 0
        self._error_count: int = 0
        self._total_count: int = 0
        self._config_profile = config_profile
        # Count of resources where the privacy evaluator detected actual PII
        # in free text (text_risk > 0) or found HIPAA-sensitive fields that
        # were not covered by any de-identification rule (identifier_risk > 0).
        # These are used by the score gate for zero-tolerance PII enforcement
        # independent of the composite score.
        self._text_risk_hits: int = 0
        self._identifier_risk_hits: int = 0
        # Accumulate per-resource config_identifier_risk so the aggregate
        # batch_privacy can report the average coverage gap across all scored
        # resources (only counted when settings was available, i.e. > 0.0 or
        # settings was passed and rules were found  tracked via _config_risk_count).
        self._config_risk_sum: float = 0.0
        self._config_risk_count: int = 0
        # Frequency of each uncovered HIPAA-sensitive path across the batch, so
        # the score gate's structured block report can name the *exact* paths
        # that leaked (not just a count). Bounded by the small fixed set of
        # HIPAA_SENSITIVE_PATHS, so unbounded growth is not a concern.
        import collections

        self._uncovered_paths: collections.Counter = collections.Counter()
        # Guards all mutable accumulators below.  ``record_resource`` and
        # ``aggregate`` may run concurrently from the parallel finalize stage
        # in the pipeline; without this lock, increments and the reservoir
        # sample would race and lose updates.
        self._lock = threading.Lock()

    def record_resource(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        settings: Any = None,
    ) -> ScoreResult:
        """Score one resource and accumulate running totals."""
        with self._lock:
            self._total_count += 1
            if "error" in deidentified:
                self._error_count += 1
                self._fail_count += 1
                self._composite_sum += 0.0
                self._min_composite = min(self._min_composite, 0.0)
                _is_error = True
            else:
                _is_error = False
        if _is_error:
            # Create a minimal FAIL result for error resources
            return ScoreResult(
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

        # Heavy scoring is intentionally outside the lock to avoid serialising
        # CPU-bound work; only the accumulation below is critical-section.
        # Use the local engine directly  score_resource() would route each
        # call to the remote scoring microservice (one HTTP POST per resource),
        # which multiplies into tens of thousands of round-trips during bulk
        # export. The remote service runs the identical algorithm; local scoring
        # is correct and orders of magnitude faster in the hot loop.
        try:
            result = _score_resource_local(
                original,
                deidentified,
                manifest_entries,
                settings,
                self._config_profile,
            )
        except Exception:
            # Scoring raised unexpectedly  _total_count was already
            # incremented in the first critical section so we must balance
            # _fail_count here, otherwise aggregate() computes totals from
            # pass+fail that are one less than _total_count.
            with self._lock:
                self._error_count += 1
                self._fail_count += 1
                self._min_composite = min(self._min_composite, 0.0)
            raise

        with self._lock:
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

            # Track zero-tolerance PII leakage independently of the composite
            # score gate.  text_risk > 0 means regex/NER found an actual PII
            # pattern (SSN, phone, email, etc.) in the de-identified output.
            # identifier_risk > 0 means a HIPAA-sensitive field existed in the
            # resource but no de-identification rule touched it.
            if result.privacy:
                if result.privacy.text_risk > 0:
                    self._text_risk_hits += 1
                if result.privacy.identifier_risk > 0:
                    self._identifier_risk_hits += 1
                    # Harvest the exact uncovered paths from the coverage
                    # evidence so the gate report can name them. ``unmatched``
                    # is already truncated to 10 per resource in privacy.py.
                    for ev in result.privacy.evidence or []:
                        if getattr(ev, "check", "") == "identifier_coverage":
                            for path in (ev.details or {}).get("unmatched", []):
                                self._uncovered_paths[path] += 1
                # Accumulate config_identifier_risk when settings was available.
                # _config_coverage() returns 0.0 both when settings=None AND when
                # all rules fired  use config_risk_count to track only cases
                # where settings was present (i.e. the evaluator had rules to check).
                if result.privacy.config_identifier_risk > 0.0:
                    self._config_risk_sum += result.privacy.config_identifier_risk
                    self._config_risk_count += 1

            # Accumulate Patient QI tuples for batch-level k-anonymity
            # (reservoir sampling).
            if deidentified.get("resourceType") == "Patient":
                self._patient_seen += 1
                qi = _get_qi_extractor()(deidentified)
                if len(self._patient_qis) < _MAX_PATIENTS:
                    self._patient_qis.append(qi)
                else:
                    j = random.randrange(self._patient_seen)
                    if j < _MAX_PATIENTS:
                        self._patient_qis[j] = qi

        return result

    def record_error(self) -> None:
        with self._lock:
            self._error_count += 1
            self._total_count += 1

    def export_state(self) -> dict:
        """Return this collector's accumulators as JSON-safe primitives.

        Scoring is batch-level: the gate's k-anonymity and identifier-coverage
        checks are only meaningful over the WHOLE job.  When partitions are
        processed outside the parent process  the ``process`` staging executor
        spawns children, and the AMQP stage consumers run on other pods entirely
         each worker owns a private collector whose state must be shipped back
        and combined, or ``aggregate()`` reports ``computed=False`` and the gate
        short-circuits into publishing unchecked data.

        The payload is deliberately plain ``dict``/``list``/scalar so it can
        cross a ``ProcessPoolExecutor`` boundary today and be persisted to the
        partition ledger for a cross-pod merge later.  It carries no PHI: the QI
        tuples are the already-generalised quasi-identifiers of the de-identified
        output, and the paths are FHIRPath strings, not values.
        """
        with self._lock:
            return {
                "config_profile": self._config_profile,
                "pass_count": self._pass_count,
                "fail_count": self._fail_count,
                "error_count": self._error_count,
                "total_count": self._total_count,
                "composite_sum": self._composite_sum,
                # inf has no JSON representation; None means "no observation".
                "min_composite": (
                    None if self._min_composite == float("inf") else self._min_composite
                ),
                "utility_sum": self._utility_sum,
                "quality_sum": self._quality_sum,
                "patient_qis": [list(qi) for qi in self._patient_qis],
                "patient_seen": self._patient_seen,
                "text_risk_hits": self._text_risk_hits,
                "identifier_risk_hits": self._identifier_risk_hits,
                "config_risk_sum": self._config_risk_sum,
                "config_risk_count": self._config_risk_count,
                "uncovered_paths": dict(self._uncovered_paths),
            }

    def merge_state(self, state: dict) -> None:
        """Fold a worker's :meth:`export_state` payload into this collector.

        Counters and sums add; ``min_composite`` takes the minimum so the worst
        resource anywhere in the job still drives the gate.  QI tuples are
        concatenated (then re-sampled to the reservoir bound) because batch
        k-anonymity must see the whole cohort  computing it on one partition's
        patients would report a reassuring k that the released dataset does not
        actually satisfy.

        Idempotency is the caller's responsibility: merging the same partition's
        state twice double-counts.  Callers merge exactly once per completed
        partition.
        """
        if not state:
            return
        with self._lock:
            self._pass_count += int(state.get("pass_count", 0))
            self._fail_count += int(state.get("fail_count", 0))
            self._error_count += int(state.get("error_count", 0))
            self._total_count += int(state.get("total_count", 0))
            self._composite_sum += float(state.get("composite_sum", 0.0))
            self._utility_sum += float(state.get("utility_sum", 0.0))
            self._quality_sum += float(state.get("quality_sum", 0.0))
            self._text_risk_hits += int(state.get("text_risk_hits", 0))
            self._identifier_risk_hits += int(state.get("identifier_risk_hits", 0))
            self._config_risk_sum += float(state.get("config_risk_sum", 0.0))
            self._config_risk_count += int(state.get("config_risk_count", 0))

            incoming_min = state.get("min_composite")
            if incoming_min is not None:
                self._min_composite = min(self._min_composite, float(incoming_min))

            for path, count in (state.get("uncovered_paths") or {}).items():
                self._uncovered_paths[path] += int(count)

            self._patient_seen += int(state.get("patient_seen", 0))
            # JSON round-trips tuples into lists; the equivalence-class grouping
            # in the k-anonymity evaluator needs hashable keys.
            for qi in state.get("patient_qis") or []:
                self._patient_qis.append(tuple(qi))
            if len(self._patient_qis) > _MAX_PATIENTS:
                # Keep the reservoir bound. A uniform sample of the union is a
                # sound estimator for the same reason the per-collector
                # reservoir is, and it keeps memory flat as partitions merge.
                self._patient_qis = random.sample(self._patient_qis, _MAX_PATIENTS)

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
            )

        # Inject the per-resource average config_identifier_risk into batch_privacy.
        # evaluate_batch_from_qis() returns config_identifier_risk=0.0 because it
        # only has QI tuples, not per-resource settings.  We correct that here
        # by substituting the average accumulated during record_resource() calls.
        avg_config_risk = (
            self._config_risk_sum / self._config_risk_count
            if self._config_risk_count > 0
            else 0.0
        )
        if batch_privacy is not None and avg_config_risk > 0.0:
            # Replace the placeholder 0.0 with the actual computed average.
            batch_privacy = PrivacyDecision(
                risk_score=batch_privacy.risk_score,
                passed=batch_privacy.passed,
                threshold=batch_privacy.threshold,
                attacker_risk=batch_privacy.attacker_risk,
                identifier_risk=batch_privacy.identifier_risk,
                config_identifier_risk=round(avg_config_risk, 4),
                text_risk=batch_privacy.text_risk,
                evidence=batch_privacy.evidence,
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
            # Zero-tolerance PII leak counters  used by the score gate for a
            # hard block independent of the composite score.
            "text_risk_hits": self._text_risk_hits,
            "identifier_risk_hits": self._identifier_risk_hits,
            # Exact HIPAA paths left uncovered, most frequent first  drives the
            # gate's structured "what leaked" block. [(path, resource_count), …]
            "uncovered_paths": self._uncovered_paths.most_common(20),
        }
