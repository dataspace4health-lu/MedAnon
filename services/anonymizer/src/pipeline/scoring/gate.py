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


def _identifier_coverage_blocks() -> bool:
    """Whether a HIPAA-coverage gap (identifier_risk) hard-blocks output.

    ``text_risk`` (detected PII in output) always blocks. ``identifier_risk`` is
    a coverage gap — set ``MEDANON_GATE_IDENTIFIER_MODE=warn`` to release output
    with a warning when the only finding is uncovered HIPAA fields and no actual
    PII pattern was detected. Mirrors the sync-path knob in ``scoring_helpers``.
    """
    from utils.regulated import gate_identifier_mode

    return gate_identifier_mode() != "warn"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ScoreGateBlocked(Exception):
    """Raised by check_score_gate() when output is blocked.

    ``str(exc)`` is the complete plain-language feedback for ``job.error``.
    ``.report`` carries the same verdict as a structured dict (issues, fixes,
    leaked-field counts, score, grade) so the job record can surface *what
    leaked and what to fix* to the UI without parsing the text blob. The output
    is still deleted and the download still blocked — only the explanation is
    machine-readable.
    """

    def __init__(self, message: str, report: dict | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


# ---------------------------------------------------------------------------
# Grade helper
# ---------------------------------------------------------------------------


def _batch_min_k(batch_privacy: dict) -> int | None:
    """Pull min_k from the batch attacker-model evidence, if present."""
    for ev in batch_privacy.get("evidence", []) or []:
        if ev.get("check") == "attacker_model_batch":
            mk = (ev.get("details") or {}).get("min_k")
            if isinstance(mk, int):
                return mk
    return None


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
    uncovered_paths = score_summary.get("uncovered_paths", []) or []

    issues: list[str] = []
    fixes: list[str] = []
    warnings: list[str] = []

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
        if _identifier_coverage_blocks():
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
        else:
            # warn mode: surface the coverage gap but do not block output. Only
            # detected PII (text_risk) withholds release.
            warnings.append(
                f"Weak config: {identifier_risk_hits:,} resource(s) ({pct}%) have "
                "HIPAA-sensitive fields not covered by any rule. Output was "
                "released because no actual PII pattern was detected, but "
                f"coverage is incomplete — review profile '{profile}'."
            )

    # ── 1c. POPULATION k-ANONYMITY GATE (authoritative attacker model) ───────
    # Re-identification risk under k-anonymity is a population property (≈ 1/k),
    # so it is measured across the cohort in ScoreCollector.aggregate(), not on
    # single records — the per-resource attacker heuristic is advisory only (see
    # privacy._attacker_model). This is the authoritative attacker-model FAIL: a
    # cohort that did not reach the safe k is withheld regardless of composite,
    # so retained quasi-identifiers can only be released when the population
    # actually protects them.
    if req_privacy and batch_privacy and batch_privacy.get("passed") is False:
        min_k = _batch_min_k(batch_privacy)
        krisk = batch_privacy.get("risk_score")
        if min_k is not None:
            detail = f"smallest equivalence class has k = {min_k} (need k ≥ 5)"
        elif isinstance(krisk, (int, float)):
            detail = f"cohort re-identification risk = {krisk:.2f} (threshold 0.30)"
        else:
            detail = "cohort re-identification risk is above the safe threshold"
        issues.append(
            "Population re-identification risk is too high — "
            f"{detail}; an attacker could single out individuals from the "
            "released quasi-identifiers (gender, birth date, postal code)"
        )
        fixes.append(
            "Generalise quasi-identifiers across the cohort: birth dates to year "
            "or decade, postal codes to a 3-digit prefix, and suppress or bucket "
            "rare gender/ethnicity combinations until every equivalence class "
            "has at least 5 patients."
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
        # No blocking issues. In warn mode an uncovered-HIPAA gap surfaces here
        # as a warning so operators still see the weak-config signal in the logs
        # / job record without the output being withheld.
        if warnings:
            _log.warning(
                "score_gate_warn profile=%s identifier_risk_hits=%d: %s",
                profile,
                identifier_risk_hits,
                " ".join(warnings),
            )
        return  # All blocking gates passed

    # ── Build the feedback message ───────────────────────────────────────────
    hard_pii = text_risk_hits > 0 or (
        identifier_risk_hits > 0 and _identifier_coverage_blocks()
    )
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

    # Structured report — same verdict, machine-readable. The job record carries
    # this so the UI can render "what leaked" + "what to fix" without parsing the
    # text blob. Output is still deleted and download still blocked.
    report = {
        "blocked": True,
        "critical_pii": hard_pii,
        "score": round(float(avg_composite), 1),
        "grade": grade_str,
        "min_required": min_comp,
        "min_grade": _grade(min_comp),
        "resources_total": total,
        "profile": profile,
        "text_risk_hits": text_risk_hits,
        "identifier_risk_hits": identifier_risk_hits,
        # [{"path": "Patient.name", "resource_count": 120}, …] — the exact
        # HIPAA-sensitive paths left uncovered, most frequent first.
        "leaked_fields": [{"path": p, "resource_count": c} for p, c in uncovered_paths],
        "issues": issues,
        "fixes": fixes,
        "message": message,
    }

    _log.warning(
        "score_gate_blocked profile=%s composite=%.1f "
        "text_risk_hits=%d identifier_risk_hits=%d fail_count=%d",
        profile,
        avg_composite,
        text_risk_hits,
        identifier_risk_hits,
        fail_count,
    )
    raise ScoreGateBlocked(message, report=report)
