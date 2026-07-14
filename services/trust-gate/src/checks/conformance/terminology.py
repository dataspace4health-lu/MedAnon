"""Code-system conformance: offline well-formedness (format + check digit) and the
server-backed terminology validation. ``code_wellformed`` is the always-available
verification companion to the ``terminology`` validation check, which runs off the
critical path with a timeout that degrades to SKIPPED (NA).
"""

from __future__ import annotations

from concurrent.futures import TimeoutError as _FuturesTimeout

from checks.code_systems import validate_code_format
from constants import CLINICAL_CODE_SYSTEMS, threshold_for
from passport import CheckResult
from terminology_client import TerminologyUnavailable

from checks.conformance._shared import (
    TERMINOLOGY_TIMEOUTS,
    _iter_codings,
    _log,
    _offpath_pool,
    _TERMINOLOGY_TIMEOUT_SEC,
)


def _code_wellformed(resources, thresholds):
    """Codes are well-formed for their system (format + check digit), checked
    offline. Verification counterpart to the server-backed terminology check:
    it catches malformed/mistyped codes (bad LOINC format, failed SNOMED Verhoeff
    digit, non-pattern ICD-10, non-numeric RxNorm) without any external server.
    Codings in systems we cannot check offline (e.g. UCUM) are not counted.
    """
    chk = CheckResult(
        check_id="conformance.code_wellformed",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.code_wellformed", thresholds),
        description="Codes are well-formed for their code system (format + check digit).",
        recommendation=(
            "Fix malformed codes — LOINC format, SNOMED Verhoeff check digit, "
            "ICD-10 pattern, numeric RxNorm."
        ),
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
    for res in resources:
        if not isinstance(res, dict):
            continue
        for coding in _iter_codings(res):
            system, code = coding.get("system"), coding.get("code")
            if not (isinstance(system, str) and isinstance(code, str) and code):
                continue
            verdict = validate_code_format(system, code)
            if verdict is None:
                continue  # system not checkable offline → NA for this coding
            chk.applicable += 1
            if not verdict:
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=res.get("id", ""),
                    path="coding",
                    detail=f"malformed code '{code}' for system {system}",
                )
    return chk


def _terminology_result(thresholds, *, skipped: bool = False, skip_reason: str = ""):
    """Construct a fresh conformance.terminology CheckResult (shared shape)."""
    return CheckResult(
        check_id="conformance.terminology",
        category="conformance",
        subcategory="value",
        context="validation",
        threshold=threshold_for("conformance.terminology", thresholds),
        description="Codes are valid per their (clinical) code system.",
        recommendation="Validate codes against the bound code systems / ValueSets.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
        skipped=skipped,
        skip_reason=skip_reason,
    )


def _terminology_sync(resources, terminology, thresholds) -> CheckResult:
    """Inner sync implementation — called inside a thread for timeout isolation."""
    chk = _terminology_result(thresholds)
    if terminology is None:
        return chk  # NA — no terminology server → no false PASS
    try:
        for res in resources:
            if not isinstance(res, dict):
                continue
            for coding in _iter_codings(res):
                system, code = coding.get("system"), coding.get("code")
                if system in CLINICAL_CODE_SYSTEMS and isinstance(code, str) and code:
                    chk.applicable += 1
                    if not terminology.validate_code(system, code):
                        chk.violations += 1
    except TerminologyUnavailable as exc:
        _log.warning("terminology not assessed — server unavailable: %s", exc)
        chk.applicable = chk.violations = 0
    return chk


def _terminology_async(resources, terminology, thresholds) -> CheckResult:
    """Run terminology validation in a background thread with timeout.

    A timeout degrades to SKIPPED (NA) — terminology latency must not stall
    the intake critical path (Snowstorm cold start: 30 s+).
    """
    if terminology is None:
        # Fast path: no server configured → NA immediately, no thread needed.
        return _terminology_result(thresholds)
    # Submit onto the persistent pool; on timeout return immediately (the abandoned
    # task drains in the background — we never block on shutdown).
    future = _offpath_pool.submit(_terminology_sync, resources, terminology, thresholds)
    try:
        return future.result(timeout=_TERMINOLOGY_TIMEOUT_SEC)
    except _FuturesTimeout:
        TERMINOLOGY_TIMEOUTS.inc()
        _log.warning(
            "terminology check timed out after %ss — marking SKIPPED",
            _TERMINOLOGY_TIMEOUT_SEC,
        )
        return _terminology_result(
            thresholds,
            skipped=True,
            skip_reason=(
                f"terminology service did not respond within {_TERMINOLOGY_TIMEOUT_SEC}s"
            ),
        )
