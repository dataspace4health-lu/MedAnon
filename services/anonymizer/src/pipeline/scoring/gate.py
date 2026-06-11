"""Score gate — blocks job output when de-identification quality is too low
or when any personal identifiers are detected in the output.

Two independent checks run in sequence:

  1. HARD PRIVACY GATE (always when gate is enabled, cannot be lowered)
     Blocks if any resource has:
       • text_risk > 0  — PII regex or NER found an actual pattern (SSN,
                          phone, email, ISO date, IP, MRN) in output text.
       • identifier_risk > 0 — a HIPAA-sensitive field exists in the resource
                               but no de-identification rule touched it.
     These checks are independent of the composite score — a job scoring
     Grade A still fails if a single resource leaks a phone number.

  2. COMPOSITE SCORE GATE (configurable threshold)
     Blocks if avg_composite < MEDANON_SCORE_GATE_MIN_COMPOSITE (default 80,
     Grade B).  Grade D (50–64%) and Grade C (65–79%) are blocked by default.

Environment variables:

    MEDANON_SCORE_GATE_ENABLED          true | false  (default: false)
    MEDANON_SCORE_GATE_MIN_COMPOSITE    0–100         (default: 80 = Grade B)
    MEDANON_SCORE_GATE_REQUIRE_PRIVACY  true | false  (default: true)
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.score_gate")


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _gate_enabled() -> bool:
    # Explicit override always wins.
    explicit = os.environ.get("MEDANON_SCORE_GATE_ENABLED", "").strip().lower()
    if explicit == "false":
        return False
    if explicit == "true":
        return True
    # No explicit override — gate is active whenever scoring is enabled.
    # MEDANON_SCORING_ENABLED=true is the only setting needed to turn this on.
    return os.environ.get("MEDANON_SCORING_ENABLED", "false").lower() == "true"


def _min_composite() -> float:
    try:
        return float(os.environ.get("MEDANON_SCORE_GATE_MIN_COMPOSITE", "80"))
    except ValueError:
        return 80.0


def _require_privacy() -> bool:
    return (
        os.environ.get("MEDANON_SCORE_GATE_REQUIRE_PRIVACY", "true").lower() != "false"
    )


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ScoreGateBlocked(Exception):
    """Raised by check_score_gate() when output is blocked.

    ``str(exc)`` is the complete plain-language feedback for ``job.error``.
    """


# ---------------------------------------------------------------------------
# Grade helper
# ---------------------------------------------------------------------------


def _grade(composite: float) -> str:
    if composite >= 90:
        return "A"
    if composite >= 80:
        return "B"
    if composite >= 65:
        return "C"
    if composite >= 50:
        return "D"
    return "F"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_score_gate(score_summary: dict | None, config_profile: str = "auto") -> None:
    """Evaluate the aggregate score and raise ScoreGateBlocked if it fails.

    Called from executor_export after output is written but before the job
    is finalised.  The caller deletes the output file if this raises.

    Does nothing when MEDANON_SCORE_GATE_ENABLED is not ``true``, or when
    ``score_summary`` is None / ``computed`` is False.
    """
    if not _gate_enabled():
        return
    if not score_summary or not score_summary.get("computed"):
        return

    min_comp = _min_composite()
    req_privacy = _require_privacy()

    avg_composite = score_summary.get("avg_composite", 100.0)
    pass_count = score_summary.get("pass_count", 0)
    fail_count = score_summary.get("fail_count", 0)
    error_count = score_summary.get("error_count", 0)
    total = pass_count + fail_count
    avg_utility = score_summary.get("avg_utility", 1.0)
    avg_quality = score_summary.get("avg_quality", 1.0)
    batch_privacy = score_summary.get("batch_privacy") or {}
    profile = score_summary.get("config_profile", config_profile)
    text_risk_hits = score_summary.get("text_risk_hits", 0)
    identifier_risk_hits = score_summary.get("identifier_risk_hits", 0)

    issues: list[str] = []
    fixes: list[str] = []

    # ── 1. HARD PII LEAK GATE (zero-tolerance, independent of score) ─────────
    # This fires even when composite = 100%.  Detected PII in the output means
    # the de-identification did not cover every field — releasing the data
    # would be a direct privacy breach.

    if text_risk_hits > 0:
        pct = round(text_risk_hits / max(total, 1) * 100, 2)
        issues.append(
            f"Personal identifiers detected in the output text of "
            f"{text_risk_hits:,} resource(s) ({pct}%) — "
            "patterns such as phone numbers, email addresses, SSNs, "
            "dates, or MRNs were found in the de-identified data"
        )
        fixes.append(
            "Enable NLP scrubbing on ALL free-text fields. Add "
            "'nlp_detect_act' or 'scrub_text' rules for: note.text, "
            "valueString, comment, text.div, and any narrative fields "
            "in the config profile."
        )
        fixes.append(
            "If the detected pattern is a clinical code mistaken for a "
            "date or phone number, check the scoring audit report "
            "(look for 'text_risk' evidence entries) to confirm whether "
            "these are true PII or false positives before re-running."
        )

    if identifier_risk_hits > 0 and req_privacy:
        pct = round(identifier_risk_hits / max(total, 1) * 100, 2)
        issues.append(
            f"{identifier_risk_hits:,} resource(s) ({pct}%) contain "
            "HIPAA-sensitive fields (name, identifier, address, contact, "
            "birth date, SSN) that were NOT covered by any "
            "de-identification rule — those values are in the output unchanged"
        )
        fixes.append(
            f"Open the config profile '{profile}' and verify rules exist "
            "for every HIPAA-sensitive field present in your FHIR data. "
            "Check the audit report (identifier_coverage section) for the "
            "exact paths that are missing rules."
        )
        fixes.append(
            "Common missed fields: Patient.identifier.value, "
            "Patient.contact.telecom.value, Practitioner.identifier.value, "
            "and Organization.contact.name. Add explicit rules for each."
        )

    # ── 2. COMPOSITE SCORE GATE ──────────────────────────────────────────────
    composite_blocked = avg_composite < min_comp
    if composite_blocked:
        grade = _grade(avg_composite)
        min_grade = _grade(min_comp)
        issues.append(
            f"Overall quality score is too low — "
            f"{avg_composite:.1f}% (Grade {grade}) "
            f"vs minimum required {min_comp:.0f}% (Grade {min_grade})"
        )

        if avg_quality < 0.90:
            issues.append(
                f"De-identification rule coverage is low "
                f"({avg_quality * 100:.1f}%) — some rules are not "
                "firing on the actual data"
            )
            fixes.append(
                "Remove rules that target fields absent from your FHIR "
                "data (they count as 'applicable but not fired'). Run the "
                "scoring audit report to identify which rules never matched."
            )

        if avg_utility < 0.80:
            issues.append(
                f"Too much information was removed ({avg_utility * 100:.1f}% "
                "data utility) — research use may be impaired"
            )
            fixes.append(
                f"The '{profile}' profile uses heavy masking "
                "(substitute / redact). Consider switching to "
                "'config_gpas.yaml' or 'config_research_pseudonymous.yaml' "
                "which use pseudonymisation (low information loss) instead."
            )

    # ── 3. PRIVACY GATE (aggregate fail count) ───────────────────────────────
    if fail_count > 0 and req_privacy and not composite_blocked:
        priv_pct = round(fail_count / max(total, 1) * 100, 1)
        issues.append(
            f"Privacy gate failed on {fail_count:,} resource(s) "
            f"({priv_pct}%) — residual re-identification risk is above "
            "the configured threshold"
        )
        if batch_privacy.get("min_k") is not None and batch_privacy["min_k"] < 5:
            mk = batch_privacy["min_k"]
            fixes.append(
                f"Patient k-anonymity is too low (min k = {mk}, required ≥ 5). "
                "Generalise birth dates to decade, zip codes to 3-digit prefix, "
                "or suppress rare gender/ethnicity combinations."
            )
        fixes.append(
            "Review the score audit report for 'identifier_coverage' and "
            "'text_risk' evidence entries to pinpoint which fields are "
            "triggering the privacy gate."
        )

    # ── 4. HIGH ERROR RATE ───────────────────────────────────────────────────
    if total > 0 and error_count / max(total, 1) > 0.05:
        err_pct = round(error_count / total * 100, 1)
        issues.append(
            f"{error_count:,} resources failed to process ({err_pct}% "
            "error rate, threshold 5%)"
        )
        fixes.append(
            "Check worker logs for the resource types with the highest "
            "error counts. Common causes: malformed FHIR resources, "
            "missing required fields, or a config rule that produces an "
            "invalid output value."
        )

    if not issues:
        return  # All gates passed

    # ── Build the feedback message ───────────────────────────────────────────
    hard_pii = text_risk_hits > 0 or identifier_risk_hits > 0
    grade_str = _grade(avg_composite)

    lines: list[str] = [
        "Output blocked — de-identification quality gate failed.",
        "",
    ]
    if hard_pii:
        lines.append(
            "CRITICAL: Personal identifiers were detected in the output. "
            "This data MUST NOT be released regardless of the composite score."
        )
        lines.append("")
    lines.append(
        f"Score: {avg_composite:.1f}% (Grade {grade_str})  |  "
        f"Resources: {total:,}  |  Profile: {profile}"
    )
    lines.append("")
    lines.append("What went wrong:")
    for i, issue in enumerate(issues, 1):
        lines.append(f"  {i}. {issue}.")

    if fixes:
        lines.append("")
        lines.append("What to fix:")
        for i, fix in enumerate(fixes, 1):
            lines.append(f"  {i}. {fix}")

    lines.extend(
        [
            "",
            "The output file has been deleted. "
            "Resolve the issues above and re-run the job.",
        ]
    )

    message = "\n".join(lines)
    _log.warning(
        "score_gate_blocked profile=%s composite=%.1f "
        "text_risk_hits=%d identifier_risk_hits=%d fail_count=%d",
        profile,
        avg_composite,
        text_risk_hits,
        identifier_risk_hits,
        fail_count,
    )
    raise ScoreGateBlocked(message)
