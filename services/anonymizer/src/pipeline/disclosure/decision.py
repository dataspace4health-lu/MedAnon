"""Export-decision engine (Statistical Disclosure Control output checking).

Applies a small set of transparent disclosure-control rules to the privacy-risk
assessment and minimisation report, returning a decision record. The rules are
deliberately explicit (no magic scores) so an output checker can audit *why* a
decision was made:

  R1 residual direct identifiers present in the output        -> REFUSE
  R2 re-identification risk level                             -> REFUSE (critical)
                                                                 REFER  (high)
  R3 minimum equivalence-class size below threshold (k)       -> REFER
  R4 synthetic record identical to a real record (leakage)    -> REFUSE
  R5 variables not justified by the declared purpose          -> REFER

The overall decision is the most restrictive rule outcome. In regulated mode a
REFER is escalated to REFUSE (fail-closed): a human output checker must resolve
it before release.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

APPROVE = "approve"
REFER = "refer"
REFUSE = "refuse"

# Restrictiveness ordering (higher = more restrictive).
_ORDER = {APPROVE: 0, REFER: 1, REFUSE: 2}

_DEFAULT_THRESHOLDS: dict[str, Any] = {
    "min_k": 5,
    "refuse_risk_levels": ("critical",),
    "refer_risk_levels": ("high",),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _worst(outcomes: list[str]) -> str:
    return max(outcomes, key=lambda o: _ORDER[o]) if outcomes else APPROVE


def assess_export_decision(
    resources: list[dict],
    *,
    privacy_risk: dict | None = None,
    minimisation: dict | None = None,
    synthetic: list[dict] | None = None,
    declared_paths: list[str] | None = None,
    thresholds: dict | None = None,
    regulated: bool | None = None,
    permit: Any = None,
    recipient: str | None = None,
    decided_by: str = "system",
) -> dict[str, Any]:
    """Return an export-decision record for *resources*.

    Missing inputs are computed: ``privacy_risk`` via
    :func:`analytics.privacy_risk.assess_privacy_risk` and ``minimisation`` via
    :func:`pipeline.minimization.assess_minimisation`.

    When a :class:`domain.permit.Permit` is supplied, the release is
    additionally bound to it (WS4 permit-scoping): it must be active, the
    *recipient* must match, and the declared variables must stay within the
    permit's authorised scope.
    """
    th = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    if regulated is None:
        from utils.regulated import regulated_mode

        regulated = regulated_mode()

    if privacy_risk is None:
        from analytics.privacy_risk import assess_privacy_risk

        privacy_risk = assess_privacy_risk(resources, synthetic=synthetic)
    if minimisation is None:
        from pipeline.minimization import assess_minimisation

        minimisation = assess_minimisation(resources, declared_paths=declared_paths)

    reid = (privacy_risk.get("re_identification") or {}).get("summary") or {}
    dist = privacy_risk.get("distance") or {}
    msum = minimisation.get("summary") or {}

    checks: list[dict[str, Any]] = []

    # R1 residual direct identifiers
    di = int(msum.get("direct_identifier_count", 0))
    if di > 0:
        checks.append(
            {
                "rule": "residual_direct_identifiers",
                "outcome": REFUSE,
                "detail": f"{di} direct identifier(s) still present in output",
            }
        )

    # R2 re-identification risk level
    risk_level = reid.get("risk_level")
    if risk_level in th["refuse_risk_levels"]:
        checks.append(
            {
                "rule": "reidentification_risk",
                "outcome": REFUSE,
                "detail": f"re-identification risk level = {risk_level}",
            }
        )
    elif risk_level in th["refer_risk_levels"]:
        checks.append(
            {
                "rule": "reidentification_risk",
                "outcome": REFER,
                "detail": f"re-identification risk level = {risk_level}",
            }
        )

    # R3 small-cell / k threshold
    min_k = reid.get("min_k")
    if isinstance(min_k, int) and min_k < th["min_k"]:
        checks.append(
            {
                "rule": "small_cell",
                "outcome": REFER,
                "detail": f"min k-anonymity {min_k} < threshold {th['min_k']}",
            }
        )

    # R4 synthetic record leakage (only when a synthetic set was compared)
    if dist.get("computed") and int(dist.get("exact_duplicates", 0)) > 0:
        checks.append(
            {
                "rule": "synthetic_leakage",
                "outcome": REFUSE,
                "detail": f"{dist['exact_duplicates']} synthetic record(s) identical to real",
            }
        )

    # R5 purpose limitation
    unj = int(msum.get("unjustified_count", 0))
    if unj > 0:
        checks.append(
            {
                "rule": "purpose_limitation",
                "outcome": REFER,
                "detail": f"{unj} variable(s) not justified by declared purpose",
            }
        )

    # R6-R8 permit binding (WS4/WS8): a release must be covered by an active,
    # in-scope permit for the named recipient.
    if permit is not None:
        if not permit.is_active():
            checks.append(
                {
                    "rule": "permit_inactive",
                    "outcome": REFUSE,
                    "detail": f"permit {permit.id} is not active ({permit.status.value})",
                }
            )
        if recipient and permit.recipient and recipient != permit.recipient:
            checks.append(
                {
                    "rule": "recipient_mismatch",
                    "outcome": REFUSE,
                    "detail": "release recipient does not match the permit recipient",
                }
            )
        out_of_scope = [p for p in (declared_paths or []) if not permit.covers_path(p)]
        if out_of_scope:
            checks.append(
                {
                    "rule": "permit_scope",
                    "outcome": REFER,
                    "detail": f"{len(out_of_scope)} variable(s) outside the permit scope",
                }
            )

    decision = _worst([c["outcome"] for c in checks])
    escalated = False
    if regulated and decision == REFER:
        decision = REFUSE
        escalated = True

    record = {
        "decision": decision,
        "escalated_by_regulated_mode": escalated,
        "checks": checks,
        "thresholds": {"min_k": th["min_k"]},
        "inputs": {
            "re_identification_risk": risk_level,
            "min_k": min_k,
            "direct_identifiers": di,
            "unjustified_variables": unj,
            "synthetic_compared": bool(dist.get("computed")),
        },
        "permit_id": getattr(permit, "id", None) if permit is not None else None,
        "decided_by": decided_by,
        "decided_at": _now_iso(),
        "regulated_mode": regulated,
    }

    # Audit the decision (PHI-safe — counts + levels only). Never break on audit.
    try:
        from utils.audit import emit as audit_emit

        audit_emit(
            "export.decision",
            actor=decided_by,
            action="disclosure_control",
            outcome=decision,
            detail={"checks": [c["rule"] for c in checks], "regulated": regulated},
        )
    except Exception:  # noqa: BLE001
        pass

    return record
