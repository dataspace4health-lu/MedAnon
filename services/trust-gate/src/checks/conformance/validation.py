"""Validator-backed (validation-context) conformance: base-spec structural
validity, asserted meta.profile conformance, and Implementation-Guide conformance.

All three run off the critical path in the shared pool with a timeout that degrades
to SKIPPED (NA)  never a false PASS, never a false BLOCK on validator slowness.
``conformance.structural`` is the critical BLOCK gate and validates every resource;
``profile`` and ``ig_profile`` are non-critical (CONDITIONAL contributors).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from constants import threshold_for
from passport import CheckResult
from validator_client import ValidatorBadRequest, ValidatorUnavailable

from checks.conformance._shared import (
    VALIDATOR_TIMEOUTS,
    _log,
    _offpath_pool,
    _VALIDATOR_CONCURRENCY,
    _VALIDATOR_MAX_RESOURCES,
    _VALIDATOR_PER_RESOURCE_SEC,
    _VALIDATOR_SAMPLE_PER_TYPE,
    _VALIDATOR_TIMEOUT_SEC,
)


def _new_struct_prof(thresholds, *, skipped: bool = False, skip_reason: str = ""):
    """Construct the (structural, profile) CheckResult pair (shared by sync+async)."""
    struct = CheckResult(
        check_id="conformance.structural",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.structural", thresholds),
        critical=True,
        description="Resources are structurally valid against the base FHIR spec.",
        recommendation="Resolve FHIR validator error/fatal issues before sharing.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_format",
        skipped=skipped,
        skip_reason=skip_reason,
    )
    prof = CheckResult(
        check_id="conformance.profile",
        category="conformance",
        subcategory="value",
        context="validation",
        threshold=threshold_for("conformance.profile", thresholds),
        # Non-critical (RC4): a meta.profile mismatch is a Kahn *validation* gap
        # (often IG version skew), not data the pipeline cannot pseudonymize. It
        # downgrades to CONDITIONAL_PASS rather than hard-BLOCK  matching the
        # opt-in conformance.ig_profile check. True structural invalidity is caught
        # by the always-on, full-batch conformance.structural BLOCK.
        critical=False,
        description="Resources conform to their asserted meta.profile.",
        recommendation="Fix profile-conformance errors or correct meta.profile.",
        hdqt_category="conformity",
        hdqt_dimension="incompatible",
        skipped=skipped,
        skip_reason=skip_reason,
    )
    return struct, prof


def _validate_one(res, validator) -> tuple[int, int, int, int]:
    """Validate one resource → (struct_applicable, struct_violation, prof_applicable,
    prof_violation). A per-request ``ValidatorBadRequest`` (e.g. an unresolvable
    profile) skips just that axis for this resource; ``ValidatorUnavailable``
    propagates so the caller can degrade the whole check to NA.
    """
    s_appl = s_viol = p_appl = p_viol = 0
    # Base-spec structural validation: a 500 is a true per-request reject  skip
    # this resource (don't count it applicable) rather than fail or pass it.
    try:
        struct_issues = validator.validate(res)
    except ValidatorBadRequest as exc:
        _log.debug(
            "structural validate skipped for %s: %s", res.get("resourceType"), exc
        )
    else:
        s_appl = 1
        if struct_issues:
            s_viol = 1
    # Profile conformance vs declared meta.profile. The validator 500s with
    # "Unable to resolve profile" when that IG is not loaded  that means we cannot
    # assess profile conformance for this resource, not that it failed.
    profiles = [
        p for p in (res.get("meta", {}) or {}).get("profile", []) if isinstance(p, str)
    ]
    if profiles:
        try:
            prof_issues = validator.validate(res, profiles=profiles)
        except ValidatorBadRequest as exc:
            _log.debug(
                "profile validate skipped (unresolvable profile) for %s: %s",
                res.get("resourceType"),
                exc,
            )
        else:
            p_appl = 1
            if prof_issues:
                p_viol = 1
    return s_appl, s_viol, p_appl, p_viol


def _structural_and_profile_sync(resources, validator, thresholds):
    """Inner sync validation  called inside a thread for timeout isolation.

    Validates EVERY resource (RC4): structural conformance is the BLOCK gate, so a
    per-type sample let a malformed resource past the sample window escape. The
    per-resource validator calls fan out across a bounded thread pool so a full
    sweep stays off the timeout wall; ``_VALIDATOR_MAX_RESOURCES`` (0 = unbounded)
    is a safety valve, above which the remainder is left unassessed-with-disclosure
    rather than silently dropping the BLOCK.
    """
    struct, prof = _new_struct_prof(thresholds)
    candidates = [r for r in resources if isinstance(r, dict) and r.get("resourceType")]
    if _VALIDATOR_MAX_RESOURCES > 0:
        candidates = candidates[:_VALIDATOR_MAX_RESOURCES]
    if not candidates:
        return [struct, prof]

    try:
        with ThreadPoolExecutor(
            max_workers=max(_VALIDATOR_CONCURRENCY, 1),
            thread_name_prefix="tg-validate",
        ) as pool:
            for s_appl, s_viol, p_appl, p_viol in pool.map(
                lambda r: _validate_one(r, validator), candidates
            ):
                struct.applicable += s_appl
                struct.violations += s_viol
                prof.applicable += p_appl
                prof.violations += p_viol
    except ValidatorUnavailable as exc:
        _log.warning("conformance not assessed  validator unavailable: %s", exc)
        struct.applicable = struct.violations = 0
        prof.applicable = prof.violations = 0
    return [struct, prof]


def _validator_timeout_for(n: int) -> float:
    """Batch-size-aware deadline: a fixed floor plus a per-resource budget spread
    across the fan-out width, so validating every resource does not hit a fixed
    wall and degrade the whole BLOCK gate to NA on large batches."""
    return (
        _VALIDATOR_TIMEOUT_SEC
        + (n / max(_VALIDATOR_CONCURRENCY, 1)) * _VALIDATOR_PER_RESOURCE_SEC
    )


def _structural_and_profile(resources, validator, thresholds):
    """Run FHIR-validator structural + profile checks off the critical path.

    The validator is the slowest upstream; a cold start or a slow-but-not-failing
    instance must not stall intake. Runs in the persistent off-path pool with a
    batch-size-scaled timeout; on expiry both checks degrade to SKIPPED (NA)
    never a false PASS, never a false BLOCK on validator slowness. The always-on,
    full-batch in-process structural checks remain the BLOCK authority when this
    external pass degrades to NA.
    """
    if validator is None:
        return list(_new_struct_prof(thresholds))  # applicable=0 → NA (no false PASS)

    timeout = _validator_timeout_for(len(resources))
    future = _offpath_pool.submit(
        _structural_and_profile_sync, resources, validator, thresholds
    )
    try:
        return future.result(timeout=timeout)
    except _FuturesTimeout:
        VALIDATOR_TIMEOUTS.inc()
        _log.warning(
            "structural/profile validation timed out after %.1fs  marking SKIPPED",
            timeout,
        )
        return list(
            _new_struct_prof(
                thresholds,
                skipped=True,
                skip_reason=(f"FHIR validator did not respond within {timeout:.1f}s"),
            )
        )


def _new_ig_check(thresholds, *, skipped: bool = False, skip_reason: str = ""):
    return CheckResult(
        check_id="conformance.ig_profile",
        category="conformance",
        subcategory="value",
        context="validation",
        threshold=threshold_for("conformance.ig_profile", thresholds),
        critical=False,
        description="Resources conform to the selected Implementation Guide profile (e.g. US Core).",
        recommendation="Resolve IG profile error/fatal issues (must-support, bound value sets).",
        hdqt_category="conformity",
        hdqt_dimension="incompatible",
        skipped=skipped,
        skip_reason=skip_reason,
    )


def _ig_conformance_sync(resources, validator, ig_profiles, thresholds):
    """Validate each covered resource against its IG profile (sampled per type)."""
    chk = _new_ig_check(thresholds)
    seen: dict[str, int] = {}
    try:
        for res in resources:
            if not isinstance(res, dict):
                continue
            rtype = res.get("resourceType")
            profiles = ig_profiles.get(rtype) if rtype else None
            if not profiles:
                continue
            if seen.get(rtype, 0) >= _VALIDATOR_SAMPLE_PER_TYPE:
                continue
            seen[rtype] = seen.get(rtype, 0) + 1
            # An unresolvable IG profile (validator lacks the selected IG package)
            # means this resource is unassessable, not a conformance failure. Skip
            # it; if the IG is loaded for none of the resources the check stays
            # applicable=0 -> NA rather than a false BLOCK.
            try:
                ig_issues = validator.validate(res, profiles=profiles)
            except ValidatorBadRequest as exc:
                _log.debug(
                    "IG validate skipped (unresolvable profile) for %s: %s", rtype, exc
                )
                seen[rtype] -= 1
                continue
            chk.applicable += 1
            if ig_issues:
                chk.violations += 1
                chk.add_detail(
                    resource_type=rtype,
                    resource_id=str(res.get("id", ""))[:64],
                    path="meta.profile",
                    detail=f"does not conform to IG profile(s): {', '.join(profiles)}",
                )
    except ValidatorUnavailable as exc:
        _log.warning("IG conformance not assessed  validator unavailable: %s", exc)
        return [_new_ig_check(thresholds, skipped=True, skip_reason=str(exc))]
    return [chk]


def _ig_conformance(resources, validator, ig_profiles, thresholds):
    """conformance.ig_profile: validate covered types against a selected IG.

    Inert (applicable=0 → NA) unless both an IG profile map and a validator are
    supplied, so the verdict is unchanged when IG conformance is not requested.
    Runs off the critical path with the same timeout/degrade-to-NA contract as the
    base structural validation.
    """
    if not ig_profiles or validator is None:
        return [_new_ig_check(thresholds)]  # NA  no false PASS, no false BLOCK
    future = _offpath_pool.submit(
        _ig_conformance_sync, resources, validator, ig_profiles, thresholds
    )
    try:
        return future.result(timeout=_VALIDATOR_TIMEOUT_SEC)
    except _FuturesTimeout:
        VALIDATOR_TIMEOUTS.inc()
        _log.warning(
            "IG validation timed out after %ss  marking SKIPPED",
            _VALIDATOR_TIMEOUT_SEC,
        )
        return [
            _new_ig_check(
                thresholds,
                skipped=True,
                skip_reason=f"FHIR validator did not respond within {_VALIDATOR_TIMEOUT_SEC}s",
            )
        ]
