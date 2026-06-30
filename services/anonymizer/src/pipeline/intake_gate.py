"""Pre-privacy intake barrier — the symmetric counterpart to ``validation.py``.

Where ``pipeline/validation.py`` gates what *leaves* the pipeline (output PII /
score barrier), this module gates what *enters* it. It calls the Trust Gate
microservice (``integrations/trust_gate/client``) to assess incoming FHIR for
structural validity, terminology, completeness, clinical plausibility, and
auditability, then returns a Quality Passport with a PASS / CONDITIONAL_PASS /
BLOCK decision.

Enforcement is governed by ``TRUST_GATE_MODE`` (mirrors
``MEDANON_GATE_IDENTIFIER_MODE``):

  - ``warn`` (default) — always assess + attach the passport; never block.
  - ``block``          — raise :class:`IntakeBlocked` when the decision is BLOCK.
  - ``off``            — no-op (also the behaviour when no service is configured).

Fail-soft: if the Trust Gate is unreachable, the verdict degrades to an advisory
CONDITIONAL_PASS — never a silent PASS, and (in block mode) never a hard block on
the gate's own outage.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

_log = logging.getLogger("medanon.intake_gate")


class IntakeBlocked(Exception):
    """Raised when the intake barrier blocks a dataset before privacy processing.

    ``str(exc)`` is the complete plain-language feedback (suitable for an HTTP
    422 detail or ``job.error``).
    """

    def __init__(self, message: str, passport: dict | None = None) -> None:
        self.passport = passport or {}
        super().__init__(message)


@dataclass
class IntakeVerdict:
    """Result of the intake barrier. ``approved`` gates ``enforce_intake``."""

    approved: bool = True
    passport: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    message: str = ""


def _mode() -> str:
    return os.environ.get("TRUST_GATE_MODE", "warn").strip().lower() or "warn"


def _degraded_passport(reason: str, resource_count: int, config_profile: str) -> dict:
    """Advisory passport used when the Trust Gate cannot be reached."""
    return {
        "decision": "CONDITIONAL_PASS",
        "overall_score": 0.0,
        "domain_scores": {},
        "blockers": [],
        "rule_results": [],
        "approved_for": [],
        "not_approved_for": ["unverified — Trust Gate unavailable"],
        "privacy_processing_allowed": True,
        "resource_count": resource_count,
        "config_profile": config_profile,
        "degraded": True,
        "degraded_reason": reason,
    }


def assess_intake(
    resources: list[dict],
    config_profile: str = "auto",
    *,
    dataset_id: str = "dataset",
    source_types: list[str] | None = None,
    provenance: dict | None = None,
    trust_profile: str | None = None,
    phases: list[str] | None = None,
    targets: list[dict] | None = None,
    intended_use: str | None = None,
    use_case: str | None = None,
    full_urls: list[str] | None = None,
) -> IntakeVerdict:
    """Assess incoming resources and return a verdict (never raises).

    A no-op (approved, empty passport) when the gate is ``off`` or no Trust Gate
    service is configured.

    ``trust_profile`` names a stored audit profile whose phase + sector-target
    selection is sent to the Trust Gate; explicit ``phases`` / ``targets`` override
    it. None → the service runs all phases with no sector breakdown (unchanged
    default behaviour).
    """
    if _mode() == "off":
        return IntakeVerdict(approved=True)

    from integrations.trust_gate.client import (
        TrustGateRemoteError,
        get_trust_gate_client,
    )

    client = get_trust_gate_client()
    if client is None:
        return IntakeVerdict(approved=True)

    valid = [r for r in resources if isinstance(r, dict)]
    if not valid:
        return IntakeVerdict(approved=True)

    if trust_profile:
        from pipeline.trust_profile import (
            resolve_intended_use,
            resolve_phases,
            resolve_targets,
            resolve_use_case,
        )

        if phases is None:
            phases = resolve_phases(trust_profile)
        if targets is None:
            targets = resolve_targets(trust_profile)
        if intended_use is None:
            intended_use = resolve_intended_use(trust_profile)
        if use_case is None:
            use_case = resolve_use_case(trust_profile)

    try:
        passport = client.assess_batch(
            valid,
            dataset_id=dataset_id,
            source_types=source_types or ["fhir"],
            config_profile=config_profile,
            provenance=provenance or {},
            phases=phases,
            targets=targets,
            intended_use=intended_use,
            use_case=use_case,
            full_urls=full_urls,
        )
    except TrustGateRemoteError as exc:
        _log.warning("intake gate degraded — Trust Gate unavailable: %s", exc)
        return IntakeVerdict(
            approved=True,
            passport=_degraded_passport(str(exc), len(valid), config_profile),
            reasons=[f"Trust Gate unavailable: {exc}"],
        )

    decision = str(passport.get("decision", "CONDITIONAL_PASS"))
    blockers = list(passport.get("blockers", []))
    score = passport.get("overall_score")

    if decision == "BLOCK" and _mode() == "block":
        message = "\n".join(
            [
                "Input blocked by the Trust Gate (pre-privacy quality barrier):",
                f"  decision=BLOCK score={score}",
                *[f"  - {b}" for b in blockers],
            ]
        )
        _log.warning(
            "intake_blocked decision=BLOCK profile=%s blockers=%d",
            config_profile,
            len(blockers),
        )
        return IntakeVerdict(
            approved=False, passport=passport, reasons=blockers, message=message
        )

    # warn mode, or CONDITIONAL_PASS / PASS — allow, attach passport.
    if decision != "PASS":
        _log.info(
            "intake_gate decision=%s score=%s profile=%s (advisory)",
            decision,
            score,
            config_profile,
        )
    return IntakeVerdict(approved=True, passport=passport)


def enforce_intake(
    resources: list[dict],
    config_profile: str = "auto",
    *,
    dataset_id: str = "dataset",
    source_types: list[str] | None = None,
    provenance: dict | None = None,
    trust_profile: str | None = None,
    phases: list[str] | None = None,
    targets: list[dict] | None = None,
    intended_use: str | None = None,
    use_case: str | None = None,
    full_urls: list[str] | None = None,
) -> dict:
    """Assess and, in ``block`` mode, raise :class:`IntakeBlocked` on a BLOCK.

    Returns the Quality Passport dict (possibly empty when the gate is a no-op)
    so callers can persist it alongside the processing run.
    """
    verdict = assess_intake(
        resources,
        config_profile,
        dataset_id=dataset_id,
        source_types=source_types,
        provenance=provenance,
        trust_profile=trust_profile,
        phases=phases,
        targets=targets,
        intended_use=intended_use,
        use_case=use_case,
        full_urls=full_urls,
    )
    if not verdict.approved:
        raise IntakeBlocked(verdict.message, verdict.passport)
    return verdict.passport
