"""Assessment service  the business logic behind the HTTP routers.

Normalises input, runs the engine with the loaded config, records the decision
metric, derives a label, and best-effort persists the passport + findings.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

from baseline import get_baseline_store
from engine import assess
from label import build_label
from metric_catalog import resolve_critical_to_quality, resolve_use_case
from rules import PlausibilityRule, validate_rules
from store import derive_findings, get_findings_store, get_passport_store
from terminology_client import get_terminology_client
from validator_client import get_validator_client

from api.config import POLICY, RULES, THRESHOLDS
from api.metrics import DECISIONS

_log = logging.getLogger("trust_gate")


def flatten(payload: Any) -> tuple[list[dict], list[str]]:
    """Normalise a single resource / Bundle / list into (resources, full_urls).

    Bundle ``entry.fullUrl`` values are preserved so the referential-integrity
    check can resolve intra-bundle ``urn:uuid:`` references.
    """
    resources: list[dict] = []
    full_urls: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            if node.get("resourceType") == "Bundle":
                for e in node.get("entry", []):
                    if isinstance(e, dict) and isinstance(e.get("resource"), dict):
                        resources.append(e["resource"])
                        fu = e.get("fullUrl")
                        if isinstance(fu, str) and fu:
                            full_urls.append(fu)
            else:
                resources.append(node)

    _walk(payload)
    return resources, full_urls


def parse_custom_rules(raw: list[dict] | None) -> list[PlausibilityRule]:
    """Parse caller-supplied custom expectations (same declarative schema as
    config/checks.yaml) into rules. Invalid rules raise 422  a custom expectation
    that does not parse must not be silently dropped. Empty/None → no extra rules."""
    if not raw:
        return []
    errors = validate_rules(raw)
    if errors:
        raise HTTPException(status_code=422, detail={"custom_rules": errors})
    return [PlausibilityRule.model_validate(r) for r in raw]


def persist(
    passport_dict: dict[str, Any],
    provider_id: str | None,
    idempotency_key: str | None = None,
) -> None:
    """Retain the passport + derive open remediation findings. Best-effort: a
    store outage must never block or fail an assessment (PDSA loop, Phase 5).

    A supplied ``idempotency_key`` makes the write idempotent (an at-least-once
    retry or a racing replica collapses onto one assessment row)."""
    store = get_passport_store()
    if store is None:
        return
    try:
        assessment_id = store.save(
            passport_dict, provider_id=provider_id, idempotency_key=idempotency_key
        )
    except Exception:  # noqa: BLE001  persistence is non-fatal
        _log.warning("passport persistence failed (non-fatal)", exc_info=True)
        return
    fstore = get_findings_store()
    if fstore is None:
        return
    try:
        for finding in derive_findings(passport_dict, assessment_id):
            fstore.create(finding)
    except Exception:  # noqa: BLE001  finding derivation is non-fatal
        _log.warning("findings derivation failed (non-fatal)", exc_info=True)


def run_assessment(
    resources: list[dict], req, full_urls: list[str] | None = None
) -> dict[str, Any]:
    """Assess *resources* under the request options, persist, and return the dict."""
    # Use-case selection (Phase 2): resolve the declared use case to a phase subset
    # + threshold tweaks. An explicit `phases` request still wins; an unknown use
    # case falls back to all phases.
    use_case = getattr(req, "use_case", None)
    uc_phases, uc_thresholds = resolve_use_case(use_case)
    phases = getattr(req, "phases", None) or uc_phases
    thresholds = {**THRESHOLDS, **uc_thresholds} if uc_thresholds else THRESHOLDS
    critical_check_ids = resolve_critical_to_quality(use_case)
    rules = RULES + parse_custom_rules(getattr(req, "custom_rules", None))

    passport = assess(
        resources,
        dataset_id=req.dataset_id,
        source_types=req.source_types,
        config_profile=req.config_profile,
        provenance=req.provenance,
        plausibility_rules=rules,
        threshold_overrides=thresholds,
        validator=get_validator_client(),
        terminology_client=get_terminology_client(),
        baseline_store=get_baseline_store(),
        definitional_bounds=POLICY["definitional_bounds"],
        concordance_rules=POLICY["concordance_rules"],
        resource_thresholds=POLICY.get("resource_thresholds"),
        phases=phases,
        full_urls=full_urls,
        targets=getattr(req, "targets", None),
        intended_use=getattr(req, "intended_use", None),
        reference=getattr(req, "reference", None),
        lifecycle_stage=getattr(req, "lifecycle_stage", "operation"),
        org_role=getattr(req, "org_role", "data-receiving"),
        critical_check_ids=critical_check_ids,
        ig=getattr(req, "ig", None),
        external_validation=getattr(req, "external_validation", True),
    )
    DECISIONS.labels(decision=passport.decision).inc()
    result = passport.to_dict()
    if use_case:
        result.get("evaluation", {})["use_case"] = use_case
    result["label"] = build_label(result)
    persist(
        result,
        getattr(req, "provider_id", None),
        getattr(req, "idempotency_key", None),
    )
    return result
