"""Unified output-validation barrier.

Historically the engine had *two* asymmetric, opt-in safety gates:

  - ``processor._run_pii_gate`` — a raw-resource PII scan (``detect_pii_fast``)
    that ran inside ``_finalize_batch`` but only when ``MEDANON_PII_GATE`` was
    set (default off).
  - ``scoring.gate.check_score_gate`` — a rich score-summary gate (hard
    PII-leak check + composite-score threshold) that ran **only** on the async
    export path, *after* the output file was written, and deleted the file on
    block.

The sync ``/process`` path could only *flag* a leak after the bytes were
already streamed.  Three paths, three behaviours, none mandatory.

This module merges both into one barrier, ``validate_output``, that every
caller invokes from the single ``_finalize_batch`` choke point.  It raises
:class:`OutputBlocked` when the output must not be released.  Both the
raw-resource scan and the score-summary gate are inputs to one verdict.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

_log = logging.getLogger("medanon.validation")


class OutputBlocked(Exception):
    """Raised when the unified validation barrier blocks output release.

    ``str(exc)`` is the complete plain-language feedback (suitable for
    ``job.error`` or an HTTP 422 detail).
    """

    def __init__(self, message: str, reasons: list[str] | None = None) -> None:
        self.reasons = reasons or []
        super().__init__(message)


@dataclass
class ValidationVerdict:
    """Result of the unified barrier.  ``approved`` is the single source of truth."""

    approved: bool = True
    reasons: list[str] = field(default_factory=list)
    message: str = ""


def _gate_enabled() -> bool:
    """Whether the unified barrier is active.

    Default is now **ON**.  Set ``MEDANON_OUTPUT_GATE_ENABLED=false`` to disable
    (e.g. for a throughput-only pipeline with externally-trusted input).  The
    legacy ``MEDANON_PII_GATE`` / ``MEDANON_SCORE_GATE_ENABLED`` flags still act
    as explicit per-check overrides inside the individual checks below.
    """
    explicit = os.environ.get("MEDANON_OUTPUT_GATE_ENABLED", "").strip().lower()
    if explicit in ("false", "0", "no"):
        return False
    return True


def _run_raw_pii_scan(results: list[dict]) -> list[str]:
    """Raw-resource PII scan (the former ``_run_pii_gate``).

    Returns a list of human-readable critical-leak reasons (empty = clean).
    Honours the legacy ``MEDANON_PII_GATE`` override: when explicitly set to
    false the raw scan is skipped (the score-summary gate still runs).
    """
    if os.environ.get("MEDANON_PII_GATE", "").strip().lower() in ("false", "0", "no"):
        return []

    valid = [r for r in results if isinstance(r, dict) and "error" not in r]
    if not valid:
        return []

    try:
        from integrations.ai.agents.pii_detector import detect_pii_fast
    except Exception:
        # The detector is optional; absence must not crash the pipeline.
        _log.debug("pii_detector_unavailable — skipping raw PII scan")
        return []

    detections = detect_pii_fast(valid)
    critical = [d for d in detections if d.get("severity") == "critical"]
    if not critical:
        return []

    n = len(critical)
    return [
        f"{n} critical personal-identifier leak(s) detected in the output "
        "by the raw-resource PII scan"
    ]


def _run_score_summary_gate(
    score_summary: dict | None, config_profile: str
) -> list[str]:
    """Score-summary gate (delegates to the existing ``check_score_gate``).

    Returns the gate's block reasons (empty = pass).  We reuse the rich,
    well-tested ``check_score_gate`` logic verbatim and translate its
    ``ScoreGateBlocked`` into our reason list so the two checks share one
    verdict.
    """
    from pipeline.scoring.gate import ScoreGateBlocked, check_score_gate

    try:
        check_score_gate(score_summary, config_profile)
    except ScoreGateBlocked as exc:
        return [str(exc)]
    return []


def validate_output(
    results: list[dict],
    score_summary: dict | None = None,
    config_profile: str = "auto",
) -> ValidationVerdict:
    """Evaluate the unified barrier over a batch of de-identified *results*.

    Inputs:
      - ``results`` — the de-identified resources (raw-resource PII scan).
      - ``score_summary`` — the aggregated score block (score-summary gate),
        when available; ``None`` skips the score-summary checks.

    Returns a :class:`ValidationVerdict`.  Callers that must hard-block should
    use :func:`enforce_output` instead, which raises :class:`OutputBlocked`.
    """
    if not _gate_enabled():
        return ValidationVerdict(approved=True)

    reasons: list[str] = []
    reasons.extend(_run_raw_pii_scan(results))
    reasons.extend(_run_score_summary_gate(score_summary, config_profile))

    if not reasons:
        return ValidationVerdict(approved=True)

    message = "\n".join(
        ["Output blocked by the de-identification validation barrier:"]
        + [f"  - {r}" for r in reasons]
    )
    _log.warning("output_blocked reasons=%d profile=%s", len(reasons), config_profile)
    return ValidationVerdict(approved=False, reasons=reasons, message=message)


def enforce_output(
    results: list[dict],
    score_summary: dict | None = None,
    config_profile: str = "auto",
) -> None:
    """Run :func:`validate_output` and raise :class:`OutputBlocked` if blocked."""
    verdict = validate_output(results, score_summary, config_profile)
    if not verdict.approved:
        raise OutputBlocked(verdict.message, verdict.reasons)
