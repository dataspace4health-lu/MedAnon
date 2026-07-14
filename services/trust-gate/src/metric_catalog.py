"""Metric catalog + use-case selector (Phase 2).

Two declarative config files under ``config/``:
  - ``metric_catalog.yaml``      a documented "metric card" per check (Metric Hub).
  - ``use_case_profiles.yaml``   the decision-tree mapping a declared use case to
                                  the metric subset (phases) + threshold tweaks.

Selection is governed and transparent: an ``intended_use`` resolves to the phases
that matter for it (quality is fitness for a declared purpose; Kahn 2016). Unknown
use cases fall back to all phases  backward compatible with ``phases=None``.
"""

from __future__ import annotations

import logging
import os

import yaml

_log = logging.getLogger("trust_gate.metric_catalog")


def _config_dir() -> str:
    env = os.environ.get("TRUST_GATE_CHECKS_PATH", "").strip()
    if env:
        return os.path.dirname(os.path.abspath(env))
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config"
    )


def _load_yaml(name: str, key: str) -> dict:
    path = os.path.join(_config_dir(), name)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        _log.warning("%s not found at %s", name, path)
        return {}
    section = doc.get(key, {})
    return section if isinstance(section, dict) else {}


def load_metric_catalog() -> dict[str, dict]:
    """check_id → metric card (title, level, applies_to, citation, doc)."""
    return _load_yaml("metric_catalog.yaml", "cards")


def load_use_case_profiles() -> dict[str, dict]:
    """use_case → {phases, thresholds, description, citation}."""
    return _load_yaml("use_case_profiles.yaml", "use_cases")


# Loaded once at import  small, static config (mirrors rules.load_config).
_CATALOG: dict[str, dict] = load_metric_catalog()
_USE_CASES: dict[str, dict] = load_use_case_profiles()


def all_cards() -> dict[str, dict]:
    return dict(_CATALOG)


def card(check_id: str) -> dict | None:
    return _CATALOG.get(check_id)


def use_cases() -> dict[str, dict]:
    return dict(_USE_CASES)


def resolve_use_case(
    use_case: str | None,
) -> tuple[list[str] | None, dict[str, float]]:
    """Resolve a declared use case to (phases, threshold_overrides).

    Returns ``(None, {})`` for an unknown/empty use case so the caller runs all
    phases with no overrides  a typo can never silently narrow the audit.
    """
    if not use_case:
        return None, {}
    profile = _USE_CASES.get(use_case.strip())
    if not profile:
        _log.info("unknown use_case %r  falling back to all phases", use_case)
        return None, {}
    phases = profile.get("phases") or None
    thresholds = profile.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        thresholds = {}
    return phases, {str(k): float(v) for k, v in thresholds.items()}


def resolve_critical_to_quality(use_case: str | None) -> list[str]:
    """Critical-to-quality check ids for a use case (RBQM; ICH E6(R3)).

    Failures on these checks weigh into a BLOCK for that use; everything else
    stays advisory/soft. Empty for an unknown/empty use case.
    """
    if not use_case:
        return []
    profile = _USE_CASES.get(use_case.strip())
    if not profile:
        return []
    ctq = profile.get("critical_to_quality") or []
    return [str(x) for x in ctq]


def reset() -> None:
    """Test hook: reload the catalog + use-case profiles from disk."""
    global _CATALOG, _USE_CASES
    _CATALOG = load_metric_catalog()
    _USE_CASES = load_use_case_profiles()
