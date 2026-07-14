"""Aggregate output-validation barrier (raw PII scan + score-summary gate).

Where each check actually runs  verified against the call graph, not intent:

- **Raw-resource PII scan.**  One implementation:
  :func:`pipeline.gate.blocking_raw_detections`.  It is reached two ways.
  :func:`pipeline.gate.run_pii_gate` calls it from ``processor._finalize_batch``,
  the choke point *every* ``process_data_batch`` caller passes through (batch
  API, NDJSON streaming, async bulk/cohort jobs, staged worker, Bundle inner
  processing) and raises :class:`pipeline.gate.PiiLeakError`.  This module's
  :func:`_run_raw_pii_scan` calls it to fold the same decision into an
  aggregate verdict.  It blocks ``critical`` (names/SSN/MRN) and ``high``
  (phone/email/street address) HIPAA direct identifiers by default  see
  ``pii_detector.block_severities``.

  Note the scan is *content*-based and only walks strings of >= 15 characters,
  so it catches residual identifiers in free text, not a leaked ``name.family``
  in a structured field.  Structural coverage is the score-summary gate's job.

- **Score-summary gate.**  Needs an aggregated ``score_summary``, which only
  exists when ``MEDANON_SCORING_ENABLED`` is set.  It runs in the API service
  layer (``scoring_helpers``), directly in ``jobs.executor_export`` and
  ``integrations.storage``, and bundled with the raw scan in
  :func:`enforce_output`.

:func:`enforce_output` is therefore *not* on the FHIR path  its only caller is
``pipeline.sources.run``, the seam that drives the non-FHIR source adapters
(HL7 v2 / CDA / DICOM / tabular) through the engine.  The FHIR path gets the raw
scan via ``run_pii_gate`` and the score gate via the callers listed above.

Both checks raise :class:`~pipeline.exceptions.OutputBlocked`;
``PiiLeakError`` is a subclass, so a handler may catch either.  Enablement
policy (including regulated mode overriding the soft-release env knobs) lives in
:mod:`utils.regulated` so the two entry points cannot disagree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pipeline.exceptions import OutputBlocked
from pipeline.gate import blocking_raw_detections
from utils.regulated import output_gate_enabled

__all__ = [
    "OutputBlocked",
    "ValidationVerdict",
    "validate_output",
    "enforce_output",
]

_log = logging.getLogger("medanon.validation")


@dataclass
class ValidationVerdict:
    """Result of the unified barrier.  ``approved`` is the single source of truth."""

    approved: bool = True
    reasons: list[str] = field(default_factory=list)
    message: str = ""


def _gate_enabled() -> bool:
    """Whether the unified barrier is active.

    Thin alias for :func:`utils.regulated.output_gate_enabled`, kept because
    callers and tests import this name.  The policy itself lives in
    ``utils.regulated`` so that ``pipeline.gate`` can consult it without
    importing this module (which would be a cycle).
    """
    return output_gate_enabled()


def _run_raw_pii_scan(results: list[dict]) -> list[str]:
    """Raw-resource PII scan, as reasons for the aggregate verdict.

    Delegates the decision to :func:`pipeline.gate.blocking_raw_detections`
    the single implementation, which also backs the ``process_data_batch``
    choke point.  Enablement (including the regulated-mode override of the
    legacy ``MEDANON_PII_GATE=false``) is decided there.
    """
    blocking = blocking_raw_detections(results)
    if not blocking:
        return []

    n = len(blocking)
    return [
        f"{n} personal-identifier leak(s) detected in the output "
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
      - ``results``  the de-identified resources (raw-resource PII scan).
      - ``score_summary``  the aggregated score block (score-summary gate),
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
