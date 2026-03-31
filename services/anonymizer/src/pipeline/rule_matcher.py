"""FHIRPath rule matching and evaluation.

Owns the compiled-FHIRPath cache, the per-rules-set rule index, and the
parameter-interpolation helpers used by the action dispatch loop.
"""

from __future__ import annotations

import logging
from copy import deepcopy

import fhirpathpy

audit_log = logging.getLogger("medanon.audit")

# ---------------------------------------------------------------------------
# FHIRPath expression cache — parse once, reuse across resources
# ---------------------------------------------------------------------------

_fhirpath_cache: dict = {}


def _evaluate_fhirpath_cached(resource: dict, expression: str) -> list:
    """Evaluate a FHIRPath expression against *resource*, caching the compiled form."""
    compiled = _fhirpath_cache.get(expression)
    if compiled is None:
        compiled = fhirpathpy.compile(expression)
        _fhirpath_cache[expression] = compiled
    return compiled(resource, [])


# ---------------------------------------------------------------------------
# Match candidate expansion
# ---------------------------------------------------------------------------

def _build_match_candidates(match_expr: str, resource: dict) -> list[str]:
    """Expand a match expression into concrete FHIRPath candidates for this resource.

    Handles ``*.field`` wildcards and ``{resourceType}`` placeholders.
    """
    if not isinstance(match_expr, str):
        return []

    candidates = [match_expr]
    if isinstance(resource, dict):
        resource_type = resource.get("resourceType")
        if resource_type:
            if match_expr.startswith("*."):
                candidates.append(f"{resource_type}{match_expr[1:]}")
            if "{resourceType}" in match_expr:
                candidates.append(match_expr.replace("{resourceType}", resource_type))

    return list(dict.fromkeys(candidates))


# ---------------------------------------------------------------------------
# Rule index by ResourceType — fast lookup without scanning all rules
# ---------------------------------------------------------------------------

_rule_index_cache: dict = {}


def _get_rule_resource_type(rule: dict) -> str:
    """Return the resource-type prefix of a rule's match expression (or ``'*'``)."""
    match = rule.get("match", "")
    if not isinstance(match, str):
        return "*"
    if match.startswith("*.") or match.startswith("{resourceType}"):
        return "*"
    dot = match.find(".")
    if dot > 0:
        return match[:dot]
    return "*"


def _build_rule_index(rules: list) -> dict[str, list]:
    """Build ``{resourceType: [rules]}`` index for fast rule lookup."""
    index: dict[str, list] = {}
    for rule in rules:
        if not isinstance(rule, dict) or "match" not in rule or "action" not in rule:
            continue
        rt = _get_rule_resource_type(rule)
        index.setdefault(rt, []).append(rule)
    return index


def _get_rules_for_resource(resource: dict, settings) -> list:
    """Return only the rules applicable to this resource's type."""
    rules = getattr(settings, "rules", [])
    rules_id = id(rules)
    if rules_id not in _rule_index_cache:
        _rule_index_cache[rules_id] = _build_rule_index(rules)
    index = _rule_index_cache[rules_id]

    resource_type = resource.get("resourceType", "") if isinstance(resource, dict) else ""
    applicable = list(index.get(resource_type, []))
    applicable.extend(index.get("*", []))
    return applicable


# ---------------------------------------------------------------------------
# Dynamic parameter interpolation
# ---------------------------------------------------------------------------

def _interpolate_dynamic(value, dynamic_settings: dict):
    """Replace ``{{key}}`` / ``${key}`` placeholders in *value* from *dynamic_settings*."""
    if isinstance(value, str):
        out = value
        for k, v in dynamic_settings.items():
            out = out.replace("{{" + str(k) + "}}", str(v))
            out = out.replace("${" + str(k) + "}", str(v))
        return out
    if isinstance(value, list):
        return [_interpolate_dynamic(x, dynamic_settings) for x in value]
    if isinstance(value, dict):
        return {k: _interpolate_dynamic(v, dynamic_settings) for k, v in value.items()}
    return value


def _resolve_rule_params(rule: dict, settings) -> dict:
    """Return rule params merged with any dynamic overrides from *settings*."""
    dynamic_settings = getattr(settings, "dynamic_rule_settings", None)
    # Skip deepcopy when no dynamic settings (the common case)
    if not isinstance(dynamic_settings, dict) or not dynamic_settings:
        return rule.get("params", {})

    params = deepcopy(rule["params"]) if "params" in rule else {}
    params = _interpolate_dynamic(params, dynamic_settings)
    for key, value in dynamic_settings.items():
        if key in params:
            params[key] = value
    return params
