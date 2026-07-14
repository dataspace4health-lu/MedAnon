"""Conformance category (Kahn 2016 §): do data values adhere to standards/formats?

Split by concern into focused modules:
  - ``presence``    offline verification: resourceType/id present, entered-in-error
                    filter, primitive format, coding structure (the BLOCK authority).
  - ``validation``  validator-backed: structural + profile + IG conformance.
  - ``terminology`` code well-formedness (offline) + terminology (server-backed).
  - ``references``  relational: literal references resolve within the batch.
  - ``_shared``     constants, timeout counters, the off-path pool, iterators.

This package's public surface (``evaluate``, ``resolve_ig_profiles``, the timeout
counters, and the individual checks reached in tests) is re-exported here.
"""

from __future__ import annotations

import logging
import os

from passport import CheckResult
from terminology_client import TerminologyClient
from validator_client import ValidatorClient

from checks.conformance._shared import TERMINOLOGY_TIMEOUTS, VALIDATOR_TIMEOUTS
from checks.conformance.presence import (
    _coding_structure,
    _resource_id_present,
    _resource_type_present,
    _status_not_entered_in_error,
    _value_format,
)
from checks.conformance.references import _normalize_ref, _reference_integrity
from checks.conformance.terminology import (
    _code_wellformed,
    _terminology_async,
    _terminology_sync,
)
from checks.conformance.validation import (
    _ig_conformance,
    _structural_and_profile,
)

_log = logging.getLogger("trust_gate.checks.conformance")

__all__ = [
    "evaluate",
    "resolve_ig_profiles",
    "VALIDATOR_TIMEOUTS",
    "TERMINOLOGY_TIMEOUTS",
    # Individual checks are re-exported for targeted tests / reuse.
    "_structural_and_profile",
    "_ig_conformance",
    "_terminology_async",
    "_terminology_sync",
    "_reference_integrity",
    "_normalize_ref",
    "_code_wellformed",
    "_value_format",
    "_coding_structure",
    "_resource_type_present",
    "_resource_id_present",
    "_status_not_entered_in_error",
]


def _ig_config_path() -> str:
    env = os.environ.get("TRUST_GATE_IG_PROFILES_PATH", "").strip()
    if env:
        return env
    base = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "..",
        "config",
    )
    return os.path.normpath(os.path.join(base, "ig_profiles.yaml"))


def resolve_ig_profiles(ig_name: str | None) -> dict[str, list[str]] | None:
    """Return the resourceType -> [profile URLs] map for the named IG, or None.

    None (the default, no IG selected) keeps the IG check inert (NA). An unknown or
    missing IG name also returns None rather than raising  IG conformance is opt-in
    and must never block on a config typo.
    """
    if not ig_name:
        return None
    try:
        import yaml

        with open(_ig_config_path(), encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except (FileNotFoundError, ValueError) as exc:  # noqa: BLE001 via narrow set
        _log.warning("ig_profiles.yaml unavailable (%s)  IG check inert", exc)
        return None
    igs = doc.get("implementation_guides") or {}
    entry = igs.get(ig_name) or {}
    profiles = entry.get("profiles") or {}
    # Normalize: every value is a list[str].
    return {
        rtype: [
            p
            for p in (vals if isinstance(vals, list) else [vals])
            if isinstance(p, str)
        ]
        for rtype, vals in profiles.items()
    }


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
    # Offline code well-formedness (format + check digit)  always runs, no server.
    out.append(_code_wellformed(resources, thresholds))
    if run_terminology:
        out.append(_terminology_async(resources, terminology, thresholds))
    out.append(_reference_integrity(resources, thresholds, full_urls))
    return out
