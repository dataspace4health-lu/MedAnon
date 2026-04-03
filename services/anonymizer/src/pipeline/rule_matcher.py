"""FHIRPath rule matching and evaluation.

Owns the compiled-FHIRPath cache, the per-rules-set rule index, and the
parameter-interpolation helpers used by the action dispatch loop.
"""

from __future__ import annotations

import logging
import re as _re
import threading
from copy import deepcopy
from functools import lru_cache

import fhirpathpy

audit_log = logging.getLogger("medanon.audit")

# ---------------------------------------------------------------------------
# FHIRPath expression cache — compile once, reuse across resources
# ---------------------------------------------------------------------------

@lru_cache(maxsize=512)
def _compile_fhirpath(expression: str):
    """Compile a FHIRPath expression (LRU-bounded, thread-safe)."""
    return fhirpathpy.compile(expression)


def _evaluate_fhirpath_cached(resource: dict, expression: str) -> list:
    """Evaluate a FHIRPath expression against *resource*, caching the compiled form."""
    compiled = _compile_fhirpath(expression)
    return compiled(resource, [])


# ---------------------------------------------------------------------------
# Expression classification — simple paths bypass fhirpathpy entirely
# ---------------------------------------------------------------------------

# Matches "Patient.name", "Observation.value.string" — typed dot-paths with no functions
_SIMPLE_PATH_RE = _re.compile(r'^[A-Z][a-zA-Z]+(\.[a-zA-Z][a-zA-Z0-9]*)+$')
# Matches "*.meta.lastUpdated", "*.text.div" — wildcard dot-paths
_WILDCARD_PATH_RE = _re.compile(r'^\*(\.[a-zA-Z][a-zA-Z0-9]*)+$')


@lru_cache(maxsize=256)
def _classify_match(expression: str) -> str:
    """Classify a match expression for fast-path routing.

    Returns:
        ``"simple"``   — typed dot-path (e.g. ``Patient.name``)
        ``"wildcard"`` — wildcard dot-path (e.g. ``*.meta.lastUpdated``)
        ``"fhirpath"`` — complex expression requiring the full FHIRPath engine
    """
    if _SIMPLE_PATH_RE.match(expression):
        return "simple"
    if _WILDCARD_PATH_RE.match(expression):
        return "wildcard"
    return "fhirpath"


def _evaluate_simple_path(resource: dict, expression: str) -> list:
    """Evaluate a simple dot-path expression via direct dict traversal.

    Returns elements in the same ``[{"path": ..., "value": ...}]`` format
    that ``fhirpathpy + .log()`` produces, so callers see no difference.
    """
    parts = expression.split(".")
    resource_type = resource.get("resourceType") if isinstance(resource, dict) else None

    # For typed paths (Patient.name), check resource type matches
    if parts[0] != "*":
        if resource_type != parts[0]:
            return []
    # For wildcard (*.field), any resource type matches

    return _traverse(resource, parts[1:], parts[0])


def _traverse(node, path_parts: list[str], prefix: str) -> list:
    """Recursively traverse *node* following *path_parts*, collecting leaf matches.

    When the final value is a list, each element is returned separately with
    the SAME path (no ``[i]`` suffix) — matching fhirpathpy ``.log()`` behavior
    where array expansion produces identical paths for every element.
    """
    if not path_parts:
        # Leaf: expand arrays into individual elements (like fhirpathpy)
        if isinstance(node, list):
            return [{"path": prefix, "value": item} for item in node]
        return [{"path": prefix, "value": node}]

    key = path_parts[0]
    remaining = path_parts[1:]

    if isinstance(node, dict):
        child = node.get(key)
        if child is None:
            return []
        child_prefix = f"{prefix}.{key}"
        if isinstance(child, list) and remaining:
            # Mid-path array: traverse each element (same prefix, no index)
            results = []
            for item in child:
                results.extend(_traverse(item, remaining, child_prefix))
            return results
        return _traverse(child, remaining, child_prefix)

    if isinstance(node, list):
        results = []
        for item in node:
            results.extend(_traverse(item, path_parts, prefix))
        return results

    return []


# ---------------------------------------------------------------------------
# Match candidate expansion
# ---------------------------------------------------------------------------

_candidates_cache: dict[tuple, list] = {}
_CANDIDATES_CACHE_MAX = 512
_cache_lock = threading.Lock()


def _build_match_candidates(match_expr: str, resource: dict) -> list[str]:
    """Expand a match expression into concrete FHIRPath candidates for this resource.

    Handles ``*.field`` wildcards and ``{resourceType}`` placeholders.
    """
    resource_type = resource.get("resourceType") if isinstance(resource, dict) else None
    cache_key = (match_expr, resource_type)
    with _cache_lock:
        cached = _candidates_cache.get(cache_key)
    if cached is not None:
        return cached

    if not isinstance(match_expr, str):
        with _cache_lock:
            _candidates_cache[cache_key] = []
        return []

    candidates = [match_expr]
    if resource_type:
        if match_expr.startswith("*."):
            candidates.append(f"{resource_type}{match_expr[1:]}")
        if "{resourceType}" in match_expr:
            candidates.append(match_expr.replace("{resourceType}", resource_type))

    result = list(dict.fromkeys(candidates))
    with _cache_lock:
        if len(_candidates_cache) >= _CANDIDATES_CACHE_MAX:
            _candidates_cache.clear()
        _candidates_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Rule index by ResourceType — fast lookup without scanning all rules
# ---------------------------------------------------------------------------

_rule_index_cache: dict = {}
_per_type_cache: dict[tuple, list] = {}
_PER_TYPE_CACHE_MAX = 256


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
    # Use a stable cache key — id(rules) can collide after GC frees the old
    # object and allocates a new one at the same address.
    rules_key = getattr(settings, "filename", None)
    with _cache_lock:
        if rules_key and rules_key in _rule_index_cache:
            index = _rule_index_cache[rules_key]
        else:
            index = _build_rule_index(rules)
            if rules_key:
                _rule_index_cache[rules_key] = index

    resource_type = resource.get("resourceType", "") if isinstance(resource, dict) else ""

    # Second-level cache: avoid rebuilding the same list for every resource of the same type
    per_type_key = (rules_key, resource_type)
    with _cache_lock:
        cached_applicable = _per_type_cache.get(per_type_key)
    if cached_applicable is not None:
        return cached_applicable

    applicable = list(index.get(resource_type, []))
    applicable.extend(index.get("*", []))
    with _cache_lock:
        if rules_key:
            if len(_per_type_cache) >= _PER_TYPE_CACHE_MAX:
                _per_type_cache.clear()
            _per_type_cache[per_type_key] = applicable
    return applicable


def clear_rule_caches() -> None:
    """Clear all module-level rule caches (called when config profiles change)."""
    _compile_fhirpath.cache_clear()
    _classify_match.cache_clear()
    with _cache_lock:
        _candidates_cache.clear()
        _rule_index_cache.clear()
        _per_type_cache.clear()


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
