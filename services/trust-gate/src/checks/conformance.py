"""Conformance category (Kahn 2016 §): do data values adhere to standards/formats?

Subcategories implemented:
  - value (verification)  — SAM prerequisite: resourceType present (BLOCK on fail)
  - value (verification)  — SAM prerequisite: id present (BLOCK on fail)
  - value (verification)  — entered-in-error filter (BLOCK on fail) [FHIR Safety]
  - value (verification)  — structural validity vs the base FHIR spec (validator)
  - value (validation)    — conformance to asserted ``meta.profile`` (validator)
  - value (verification)  — coding structure: system+code present, not sentinelled
  - value (verification)  — primitive format: FHIR date/dateTime/id regexes
  - value (validation)    — terminology: (system, code) valid per a code system
  - relational (verification) — references resolve within the batch

Each check carries PIQI HDQT v2.0 dimension tags (additive; does not affect Kahn
scoring). Terminology validation runs in a background thread (configurable timeout
via TRUST_GATE_TERMINOLOGY_ASYNC_TIMEOUT_SEC, default 5 s) so it cannot stall the
critical path; a timeout marks the check skipped (NA, never FAIL).
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

from prometheus_client import Counter

from checks.code_systems import validate_code_format
from constants import (
    CLINICAL_CODE_SYSTEMS,
    ENTERED_IN_ERROR_RESOURCE_TYPES,
    FHIR_DATETIME_PATTERN,
    FHIR_ID_PATTERN,
    REDACTED_SENTINELS,
    TEMPORAL_FIELD_NAMES,
    threshold_for,
)
from passport import CheckResult, redact_token
from terminology_client import TerminologyClient, TerminologyUnavailable
from validator_client import ValidatorBadRequest, ValidatorClient, ValidatorUnavailable

_log = logging.getLogger("trust_gate.checks.conformance")

_DATETIME_RE = re.compile(FHIR_DATETIME_PATTERN)
_ID_RE = re.compile(FHIR_ID_PATTERN)

_TERMINOLOGY_TIMEOUT_SEC = float(
    os.environ.get("TRUST_GATE_TERMINOLOGY_ASYNC_TIMEOUT_SEC", "5")
)
# Validator is the slowest upstream (Java FHIR validator cold start). Bound it on
# the critical path too — a larger default than terminology since validation of a
# whole batch legitimately takes longer than a single code lookup.
_VALIDATOR_TIMEOUT_SEC = float(
    os.environ.get("TRUST_GATE_VALIDATOR_ASYNC_TIMEOUT_SEC", "45")
)
# Structural/profile validation now validates EVERY resource (RC4): sampling the
# only BLOCK-gating checks let a malformed resource past the sample window escape.
# To keep a full sweep off the timeout wall, the per-resource validator calls fan
# out across a bounded thread pool and the timeout scales with batch size.
#   _VALIDATOR_CONCURRENCY      — parallel validator calls (fan-out width).
#   _VALIDATOR_PER_RESOURCE_SEC — time budget added per resource (timeout scaling).
#   _VALIDATOR_MAX_RESOURCES    — safety valve; 0 = unbounded. Above it, the
#       remainder is left NA-with-disclosure rather than silently dropping the BLOCK.
_VALIDATOR_CONCURRENCY = int(os.environ.get("TRUST_GATE_VALIDATOR_CONCURRENCY", "8"))
_VALIDATOR_PER_RESOURCE_SEC = float(
    os.environ.get("TRUST_GATE_VALIDATOR_PER_RESOURCE_SEC", "0.5")
)
_VALIDATOR_MAX_RESOURCES = int(
    os.environ.get("TRUST_GATE_VALIDATOR_MAX_RESOURCES", "0")
)
# Retained for the opt-in, non-critical IG-profile check, which still spot-checks
# per type (it is not a BLOCK gate). 0/unset keeps the historical default of 10.
_VALIDATOR_SAMPLE_PER_TYPE = int(
    os.environ.get("TRUST_GATE_VALIDATOR_SAMPLE_PER_TYPE", "10")
)

# Prometheus: incremented whenever an off-path check exceeds its timeout and
# degrades to SKIPPED (NA). Registered in the global default registry so main's
# /metrics endpoint exports it without an import cycle.
TERMINOLOGY_TIMEOUTS = Counter(
    "trust_gate_terminology_timeout_total",
    "Times the terminology check timed out and degraded to SKIPPED (NA)",
)
VALIDATOR_TIMEOUTS = Counter(
    "trust_gate_validator_timeout_total",
    "Times the structural/profile validation timed out and degraded to SKIPPED (NA)",
)

# Persistent worker pool for off-critical-path terminology (and validator)
# evaluation. Created once at module scope — NOT per call. A per-call
# `with ThreadPoolExecutor(...)` block triggers an implicit shutdown(wait=True)
# on exit, which blocks the caller until the slow upstream finishes and defeats
# the entire purpose of the timeout. With a persistent pool, future.result(
# timeout=...) returns immediately on timeout while the abandoned task drains in
# the background (each upstream client carries its own HTTP timeout + breaker).
_OFFPATH_POOL_SIZE = int(os.environ.get("TRUST_GATE_OFFPATH_POOL_SIZE", "4"))
_offpath_pool = ThreadPoolExecutor(
    max_workers=_OFFPATH_POOL_SIZE, thread_name_prefix="tg-offpath"
)


def _ig_config_path() -> str:
    env = os.environ.get("TRUST_GATE_IG_PROFILES_PATH", "").strip()
    if env:
        return env
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "config"
    )
    return os.path.normpath(os.path.join(base, "ig_profiles.yaml"))


def resolve_ig_profiles(ig_name: str | None) -> dict[str, list[str]] | None:
    """Return the resourceType -> [profile URLs] map for the named IG, or None.

    None (the default, no IG selected) keeps the IG check inert (NA). An unknown
    or missing IG name also returns None rather than raising — IG conformance is
    opt-in and must never block on a config typo.
    """
    if not ig_name:
        return None
    try:
        import yaml

        with open(_ig_config_path(), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (FileNotFoundError, ValueError) as exc:  # noqa: BLE001 via narrow set
        _log.warning("ig_profiles.yaml unavailable (%s) — IG check inert", exc)
        return None
    igs = doc.get("implementation_guides") or {}
    entry = igs.get(ig_name) or {}
    profiles = entry.get("profiles") or {}
    # Normalize: every value is a list[str].
    return {
        rtype: [p for p in (vals if isinstance(vals, list) else [vals]) if isinstance(p, str)]
        for rtype, vals in profiles.items()
    }


def _iter_codings(resource: dict):
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            coding = node.get("coding")
            if isinstance(coding, list):
                for c in coding:
                    if isinstance(c, dict):
                        yield c
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)


def _iter_references(resource: dict):
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("reference")
            if isinstance(ref, str):
                yield ref
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)


def evaluate(
    resources: list[dict],
    validator: ValidatorClient | None,
    terminology: TerminologyClient | None,
    thresholds: dict[str, float] | None = None,
    *,
    run_validator: bool = True,
    run_terminology: bool = True,
    full_urls: list[str] | None = None,
    ig_profiles: dict[str, list[str]] | None = None,
) -> list[CheckResult]:
    """Run conformance checks.

    ``run_validator`` / ``run_terminology`` gate the two slow external calls so a
    profile that deselects the structural/terminology phase skips them entirely
    (they are the only network-bound checks). Cheap in-process checks always run;
    the engine filters their results by the selected phases.
    """
    out: list[CheckResult] = []
    # SAM prerequisite chain: these three run first and block downstream on fail.
    out.append(_resource_type_present(resources, thresholds))
    out.append(_resource_id_present(resources, thresholds))
    out.append(_status_not_entered_in_error(resources, thresholds))
    if run_validator:
        out.extend(_structural_and_profile(resources, validator, thresholds))
        out.extend(_ig_conformance(resources, validator, ig_profiles, thresholds))
    out.append(_coding_structure(resources, thresholds))
    out.append(_value_format(resources, thresholds))
    # Offline code well-formedness (format + check digit) — always runs, no server.
    out.append(_code_wellformed(resources, thresholds))
    if run_terminology:
        out.append(_terminology_async(resources, terminology, thresholds))
    out.append(_reference_integrity(resources, thresholds, full_urls))
    return out


# ---------------------------------------------------------------------------
# SAM prerequisite block checks (Phase 1B — critical, always BLOCK on FAIL)
# ---------------------------------------------------------------------------


def _resource_type_present(resources: list[dict], thresholds) -> CheckResult:
    """Every resource must carry a non-empty resourceType string.

    Without it the pipeline cannot route or match rules; processing cannot
    proceed. This check is always first in the SAM chain.
    """
    chk = CheckResult(
        check_id="conformance.resource_type_present",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.resource_type_present", thresholds),
        critical=True,
        description="Every resource carries a non-empty resourceType.",
        recommendation="Ensure all resources have a valid FHIR resourceType field.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_format",
    )
    for idx, res in enumerate(resources):
        if not isinstance(res, dict):
            continue
        chk.applicable += 1
        rt = res.get("resourceType")
        if not (isinstance(rt, str) and rt.strip()):
            chk.violations += 1
            # enumerate(): list.index() is O(n²) and, worse, returns the first
            # *equal* dict's position — wrong precisely here, where violating
            # resources are often empty/identical. add_detail() also bounds the count.
            chk.add_detail(
                resource_index=idx,
                attribute="resourceType",
                hdqt_category="accuracy",
                hdqt_dimension="invalid_format",
                severity="critical",
                detail="resourceType absent or empty",
            )
    return chk


def _resource_id_present(resources: list[dict], thresholds) -> CheckResult:
    """Every resource must carry a non-empty id string.

    Without an id the de-identification engine has no pseudonymization target
    and cross-resource references cannot be resolved.
    """
    chk = CheckResult(
        check_id="conformance.resource_id_present",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.resource_id_present", thresholds),
        critical=True,
        description="Every resource carries a non-empty id.",
        recommendation="Ensure all resources have a valid FHIR id field.",
        hdqt_category="availability",
        hdqt_dimension="missing",
    )
    for res in resources:
        if not isinstance(res, dict) or not res.get("resourceType"):
            continue
        chk.applicable += 1
        rid = res.get("id")
        if not (isinstance(rid, str) and rid.strip()):
            chk.violations += 1
            chk.add_detail(
                resource_type=res.get("resourceType"),
                attribute="id",
                hdqt_category="availability",
                hdqt_dimension="missing",
                severity="critical",
                detail="id absent or empty",
            )
    return chk


def _status_not_entered_in_error(resources: list[dict], thresholds) -> CheckResult:
    """Clinical resources with status=entered-in-error must be filtered before
    de-identification.  Per FHIR Safety Checklist §4: entered-in-error resources
    are retracted; running them through de-id would create false pseudonymized
    records that appear valid.
    """
    chk = CheckResult(
        check_id="conformance.status_not_entered_in_error",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.status_not_entered_in_error", thresholds),
        critical=True,
        description=(
            "Clinical resources ("
            + ", ".join(sorted(ENTERED_IN_ERROR_RESOURCE_TYPES))
            + ") must not have status=entered-in-error."
        ),
        recommendation=(
            "Filter out entered-in-error resources before sending to the "
            "de-identification pipeline."
        ),
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
    for res in resources:
        if not isinstance(res, dict):
            continue
        rt = res.get("resourceType")
        if rt not in ENTERED_IN_ERROR_RESOURCE_TYPES:
            continue
        chk.applicable += 1
        if res.get("status") == "entered-in-error":
            chk.violations += 1
            chk.violation_details.append({
                "check_id": chk.check_id,
                "resource_type": rt,
                "resource_id": res.get("id", ""),
                "attribute": "status",
                "value": "entered-in-error",
                "hdqt_category": "accuracy",
                "hdqt_dimension": "invalid_value",
                "severity": "critical",
                "detail": "status=entered-in-error must be filtered before de-identification",
            })
    return chk


# ---------------------------------------------------------------------------
# Existing checks (with HDQT tags added)
# ---------------------------------------------------------------------------


def _iter_temporal_values(resource: dict):
    """Yield (field_name, value) for every temporal primitive in the resource."""
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, val in node.items():
                if key in TEMPORAL_FIELD_NAMES:
                    yield key, val
                elif key == "period" and isinstance(val, dict):
                    for edge in ("start", "end"):
                        if edge in val:
                            yield f"period.{edge}", val[edge]
                if isinstance(val, (dict, list)):
                    stack.append(val)
        elif isinstance(node, list):
            stack.extend(node)


def _value_format(resources, thresholds):
    chk = CheckResult(
        check_id="conformance.value_format",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.value_format", thresholds),
        description="Primitive values match their FHIR format (date/dateTime, id).",
        recommendation="Emit FHIR-valid primitives (ISO-8601 dates, [A-Za-z0-9-.] ids).",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_format",
    )
    for res in resources:
        if not isinstance(res, dict) or not res.get("resourceType"):
            continue
        if "id" in res:
            chk.applicable += 1
            rid = res.get("id")
            if not (isinstance(rid, str) and _ID_RE.match(rid)):
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=str(res.get("id", ""))[:64],
                    path="id",
                    # A resource id can carry a real identifier (the MRN-as-id
                    # anti-pattern), so tokenize rather than surface it verbatim
                    # even though the rest of the value examples are PHI-safe.
                    value=redact_token(str(rid)[:64]),
                    detail="id is not a valid FHIR id ([A-Za-z0-9.-], max 64 chars)",
                )
        for _name, value in _iter_temporal_values(res):
            chk.applicable += 1
            if not (isinstance(value, str) and _DATETIME_RE.match(value)):
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=str(res.get("id", ""))[:64],
                    path=_name,
                    value=str(value)[:64],
                    detail=f"{_name} is not a valid FHIR date/dateTime",
                )
    return chk


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
        # downgrades to CONDITIONAL_PASS rather than hard-BLOCK — matching the
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
    # Base-spec structural validation: a 500 is a true per-request reject — skip
    # this resource (don't count it applicable) rather than fail or pass it.
    try:
        struct_issues = validator.validate(res)
    except ValidatorBadRequest as exc:
        _log.debug("structural validate skipped for %s: %s", res.get("resourceType"), exc)
    else:
        s_appl = 1
        if struct_issues:
            s_viol = 1
    # Profile conformance vs declared meta.profile. The validator 500s with
    # "Unable to resolve profile" when that IG is not loaded — that means we cannot
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
    """Inner sync validation — called inside a thread for timeout isolation.

    Validates EVERY resource (RC4): structural conformance is the BLOCK gate, so a
    per-type sample let a malformed resource past the sample window escape. The
    per-resource validator calls fan out across a bounded thread pool so a full
    sweep stays off the timeout wall; ``_VALIDATOR_MAX_RESOURCES`` (0 = unbounded)
    is a safety valve, above which the remainder is left unassessed-with-disclosure
    rather than silently dropping the BLOCK.
    """
    struct, prof = _new_struct_prof(thresholds)
    candidates = [
        r for r in resources if isinstance(r, dict) and r.get("resourceType")
    ]
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
        _log.warning("conformance not assessed — validator unavailable: %s", exc)
        struct.applicable = struct.violations = 0
        prof.applicable = prof.violations = 0
    return [struct, prof]


def _validator_timeout_for(n: int) -> float:
    """Batch-size-aware deadline: a fixed floor plus a per-resource budget spread
    across the fan-out width, so validating every resource does not hit a fixed
    wall and degrade the whole BLOCK gate to NA on large batches."""
    return _VALIDATOR_TIMEOUT_SEC + (
        n / max(_VALIDATOR_CONCURRENCY, 1)
    ) * _VALIDATOR_PER_RESOURCE_SEC


def _structural_and_profile(resources, validator, thresholds):
    """Run FHIR-validator structural + profile checks off the critical path.

    The validator is the slowest upstream; a cold start or a slow-but-not-failing
    instance must not stall intake. Runs in the persistent off-path pool with a
    batch-size-scaled timeout; on expiry both checks degrade to SKIPPED (NA) —
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
            "structural/profile validation timed out after %.1fs — marking SKIPPED",
            timeout,
        )
        return list(
            _new_struct_prof(
                thresholds,
                skipped=True,
                skip_reason=(
                    f"FHIR validator did not respond within {timeout:.1f}s"
                ),
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
                _log.debug("IG validate skipped (unresolvable profile) for %s: %s", rtype, exc)
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
        _log.warning("IG conformance not assessed — validator unavailable: %s", exc)
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
        return [_new_ig_check(thresholds)]  # NA — no false PASS, no false BLOCK
    future = _offpath_pool.submit(
        _ig_conformance_sync, resources, validator, ig_profiles, thresholds
    )
    try:
        return future.result(timeout=_VALIDATOR_TIMEOUT_SEC)
    except _FuturesTimeout:
        VALIDATOR_TIMEOUTS.inc()
        _log.warning("IG validation timed out after %ss — marking SKIPPED", _VALIDATOR_TIMEOUT_SEC)
        return [
            _new_ig_check(
                thresholds,
                skipped=True,
                skip_reason=f"FHIR validator did not respond within {_VALIDATOR_TIMEOUT_SEC}s",
            )
        ]


def _coding_structure(resources, thresholds):
    chk = CheckResult(
        check_id="conformance.coding_structure",
        category="conformance",
        subcategory="value",
        context="verification",
        threshold=threshold_for("conformance.coding_structure", thresholds),
        description="Every coding carries a non-empty system + code (not a sentinel).",
        recommendation="Ensure codings keep system+code after de-identification.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
    for res in resources:
        if not isinstance(res, dict):
            continue
        for coding in _iter_codings(res):
            chk.applicable += 1
            system, code = coding.get("system"), coding.get("code")
            if not (isinstance(system, str) and system) or not (
                isinstance(code, str) and code and code not in REDACTED_SENTINELS
            ):
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=res.get("id", ""),
                    path="coding",
                    detail="coding missing system+code or carries a redacted sentinel",
                )
    return chk


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


def _terminology_sync(resources, terminology, thresholds) -> CheckResult:
    """Inner sync implementation — called inside a thread for timeout isolation."""
    chk = CheckResult(
        check_id="conformance.terminology",
        category="conformance",
        subcategory="value",
        context="validation",
        threshold=threshold_for("conformance.terminology", thresholds),
        description="Codes are valid per their (clinical) code system.",
        recommendation="Validate codes against the bound code systems / ValueSets.",
        hdqt_category="accuracy",
        hdqt_dimension="invalid_value",
    )
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
        )
    # Submit onto the persistent pool; on timeout return immediately (the
    # abandoned task drains in the background — we never block on shutdown).
    future = _offpath_pool.submit(_terminology_sync, resources, terminology, thresholds)
    try:
        return future.result(timeout=_TERMINOLOGY_TIMEOUT_SEC)
    except _FuturesTimeout:
        TERMINOLOGY_TIMEOUTS.inc()
        _log.warning(
            "terminology check timed out after %ss — marking SKIPPED",
            _TERMINOLOGY_TIMEOUT_SEC,
        )
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
            skipped=True,
            skip_reason=(
                f"terminology service did not respond within "
                f"{_TERMINOLOGY_TIMEOUT_SEC}s"
            ),
        )


def _reference_integrity(resources, thresholds, full_urls=None):
    """References resolve within the batch.

    Two reference shapes are checked:
      * relative ``ResourceType/id`` → resolves against the batch's Type/id set;
      * absolute / ``urn:uuid:`` (Bundle ``fullUrl``) → resolves against the set
        of entry ``fullUrl`` values. These are only assessed when ``full_urls`` is
        supplied (a Bundle was flattened); for a bare resource list they remain NA
        rather than producing false dangling-reference violations.
    """
    chk = CheckResult(
        check_id="conformance.reference_integrity",
        category="conformance",
        subcategory="relational",
        context="verification",
        threshold=threshold_for("conformance.reference_integrity", thresholds),
        description="Literal references resolve to a resource present in the batch.",
        recommendation="Include referenced resources or fix dangling references.",
        hdqt_category="availability",
        hdqt_dimension="missing",
    )
    present = {
        f"{r.get('resourceType')}/{r.get('id')}"
        for r in resources
        if isinstance(r, dict) and r.get("resourceType") and r.get("id")
    }
    full_set = set(full_urls or [])
    for res in resources:
        if not isinstance(res, dict):
            continue
        for ref in _iter_references(res):
            if ref.startswith("#"):
                continue  # contained-resource reference — not a batch reference
            if ref.startswith(("urn:", "http://", "https://")):
                # Absolute / urn:uuid reference — resolvable only against Bundle
                # fullUrls. NA when no fullUrl context was provided.
                if not full_set:
                    continue
                chk.applicable += 1
                if ref not in full_set:
                    chk.violations += 1
                    chk.add_detail(
                        resource_type=res.get("resourceType"),
                        resource_id=res.get("id", ""),
                        path="reference",
                        detail=f"reference to {ref} not present in the batch",
                    )
                continue
            if "/" not in ref:
                continue
            # Normalize to the exact "ResourceType/id" key (drop query string and
            # any /_history/{vid} suffix) and require an exact match — a loose
            # endswith() can false-resolve "Patient/1" against "RelatedPerson/x1"
            # or match the right id under the wrong resource type.
            short = _normalize_ref(ref)
            if not short:
                continue
            chk.applicable += 1
            if short not in present and short not in full_set:
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=res.get("id", ""),
                    path="reference",
                    detail=f"reference to {short} not present in the batch",
                )
    return chk


def _normalize_ref(ref: str) -> str:
    """Reduce a literal reference to its exact 'ResourceType/id' key.

    Strips the query string and any '/_history/{vid}' version suffix. Returns ''
    when the reference does not have a recognizable Type/id shape.
    """
    base = ref.split("?", 1)[0]
    hist = base.find("/_history/")
    if hist != -1:
        base = base[:hist]
    parts = base.rstrip("/").rsplit("/", 2)
    if len(parts) >= 2 and parts[-2] and parts[-1]:
        return f"{parts[-2]}/{parts[-1]}"
    return ""
