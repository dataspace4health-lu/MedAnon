"""Privacy Transformation Passport (TEHDAS2 D7.2 §5.5.1, Arts 78/79).

A machine-readable, **dataset/job-level** documentation bundle that travels with
an anonymised or synthetic dataset so a downstream HDAB/data-user can evaluate
privacy risk and make (or audit) a disclosure decision. This complements the
per-resource transformation manifest in :mod:`pipeline.manifest` (which records
which rules fired on each resource); the passport summarises the *whole run*.

D7.2 §5.5.1 asks the documentation to cover, where applicable:
  - identification (data creator, permit id)
  - original-dataset metadata (size, provenance, quality/utility label — Art 78)
  - processing (steps, tools + versions, DP parameters, quality metrics)
  - privacy-risk assessment (methods, thresholds, results)
  - disclosure (decision, recipient, restrictions)

The passport MUST be anonymous — it never carries PHI or record-level values.
It is a pure builder (no I/O) so it is trivially testable and can be attached to
a job result, written as a sidecar, or embedded in a Bundle by the caller.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

PASSPORT_VERSION = "1.0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_key_id() -> str:
    """Active HMAC key-id (``MEDANON_HASH_KEY_ID``) — see ``utils.crypto``."""
    try:
        from utils.crypto import hash_key_id

        return hash_key_id()
    except Exception:  # noqa: BLE001
        return "v1"


def _tool_identity() -> dict[str, str]:
    """Reproducibility stamp for the engine (matches the /metrics BUILD_INFO)."""
    return {
        "name": "medanon",
        "version": os.environ.get("MEDANON_VERSION", "2.0.0"),
        "git_sha": os.environ.get("MEDANON_GIT_SHA", "unknown"),
    }


def _generalization_summary(plan: Any) -> dict[str, Any] | None:
    """Summarise a lattice ``GeneralizationPlan`` (or its checkpoint dict)."""
    if plan is None:
        return None

    def _get(name: str, default: Any = None) -> Any:
        if isinstance(plan, dict):
            return plan.get(name, default)
        return getattr(plan, name, default)

    return {
        "achieved_k": _get("achieved_k"),
        "achieved_l": _get("achieved_l"),
        "achieved_t": _get("achieved_t"),
        "suppressed_count": _get("suppressed_count"),
        "suppression_rate": _get("suppression_rate"),
        "information_loss": _get("information_loss"),
        "feasible": _get("feasible"),
    }


def build_transformation_passport(
    *,
    job_id: str | None = None,
    permit_id: str | None = None,
    data_creator: str = "medanon",
    config_profile: str | None = None,
    dataset_stats: dict[str, Any] | None = None,
    privacy_model: dict | None = None,
    generalization_plan: Any = None,
    privacy_risk: dict | None = None,
    dp_params: dict | None = None,
    disclosure: dict | None = None,
    extra_tools: list[dict] | None = None,
) -> dict[str, Any]:
    """Assemble a Transformation Passport dict.

    All arguments are optional so the passport can be built incrementally as a
    run progresses; absent sections are recorded as ``None`` rather than omitted
    (so consumers can distinguish "not applicable" from "not yet known").

    Parameters
    ----------
    dataset_stats:
        Anonymous original-dataset metadata (e.g. ``{"total_resources": N,
        "resource_types": {...}, "provenance": {...}, "quality_label": ...}``).
    privacy_model:
        The validated ``privacy_model`` (k/l/t targets, QIs) — the *intent*.
    generalization_plan:
        A lattice ``GeneralizationPlan`` or its checkpoint dict — the *achieved*
        guarantee.
    privacy_risk:
        Output of :func:`analytics.privacy_risk.assess_privacy_risk` — methods +
        thresholds + results (§5.5.7).
    dp_params:
        Differential-privacy parameters (``{"epsilon":…, "delta":…}``) or None.
        (Hook for WS5; DP itself is phased later.)
    disclosure:
        Disclosure-decision record (``{"decision":…, "recipient":…,
        "restrictions":…}``) or None (populated by the export workflow, WS6).
    """
    tools = [_tool_identity()]
    if extra_tools:
        tools.extend(extra_tools)

    # D7.2 §5.5.8: assess the tools used against the approved-tool registry, so
    # the passport documents that every tool was HDAB-approved (or flags the
    # ones that were not). Guarded — never let registry evaluation break the
    # passport build.
    tool_assessment: dict[str, Any] | None = None
    try:
        from pipeline.governance.tool_registry import (
            assess_tools,
            default_tool_registry,
        )

        tool_assessment = assess_tools(tools, default_tool_registry())
    except Exception:  # noqa: BLE001
        tool_assessment = None

    return {
        "passport_version": PASSPORT_VERSION,
        "generated_at": _now_iso(),
        "identification": {
            "data_creator": data_creator,
            "permit_id": permit_id,
            "job_id": job_id,
            # D7.2 §4.2 key rotation: stamp the active HMAC key-id so a permit's
            # pseudonyms are attributable to the key generation that produced
            # them (a rotation changes this, making it traceable).
            "hash_key_id": _hash_key_id(),
        },
        "original_dataset": dataset_stats,
        "processing": {
            "config_profile": config_profile,
            "tools": tools,
            "tool_assessment": tool_assessment,
            "privacy_model": privacy_model,
            "generalization": _generalization_summary(generalization_plan),
            "differential_privacy": dp_params,
        },
        "privacy_risk_assessment": privacy_risk,
        "disclosure": disclosure,
    }
