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

audit_log = logging.getLogger("medanon.audit")

# ---------------------------------------------------------------------------
# FHIRPath expression cache — compile once, reuse across resources
# ---------------------------------------------------------------------------

_fhirpathpy_log_registered = False
_fhirpathpy_log_lock = threading.Lock()


@lru_cache(maxsize=512)
def _compile_fhirpath(expression: str):
    """Compile a FHIRPath expression (LRU-bounded, thread-safe).

    Lazy-imports ``fhirpathpy`` so the heavy antlr4 grammar only loads when
    a truly complex expression is encountered (not on module import).
    """
    global _fhirpathpy_log_registered
    import fhirpathpy
    if not _fhirpathpy_log_registered:
        with _fhirpathpy_log_lock:
            if not _fhirpathpy_log_registered:
                fhirpathpy.engine.invocations["log"] = {
                    "fn": lambda ctx, els: [{"path": x.path, "value": x.data} for x in els]
                }
                _fhirpathpy_log_registered = True
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
        ``"where"``    — ``.where(url=...)`` expression handled natively
        ``"fhirpath"`` — complex expression requiring the full FHIRPath engine
    """
    if _SIMPLE_PATH_RE.match(expression):
        return "simple"
    if _WILDCARD_PATH_RE.match(expression):
        return "wildcard"
    if _parse_where_plan(expression) is not None:
        return "where"
    return "fhirpath"


# ---------------------------------------------------------------------------
# Native .where(url=...) evaluator — eliminates fhirpathpy for extensions
# ---------------------------------------------------------------------------

# Matches segments like: .where(url='...') or .where(url.startsWith('...'))
_WHERE_SEGMENT_RE = _re.compile(
    r"\.where\("
    r"(?:"
    r"url='([^']+)'"                          # group 1: exact url value
    r"|"
    r"url\.startsWith\('([^']+)'\)"           # group 2: prefix value
    r")"
    r"\)"
)


@lru_cache(maxsize=128)
def _parse_where_plan(expression: str):
    """Parse a ``.where(url=...)``-containing expression into a traversal plan.

    Returns a tuple ``(resource_type, segments, trailing)`` where:
      - *resource_type* is the leading FHIR type (e.g. ``"Patient"``)
      - *segments* is a tuple of ``(path_keys, filter_value, filter_mode)``
      - *trailing* is a tuple of remaining dot-path keys after the last ``.where()``

    Returns ``None`` if the expression cannot be decomposed.
    """
    # Must start with ResourceType
    if not expression or not expression[0].isupper():
        return None
    # Must contain at least one .where(
    if ".where(" not in expression:
        return None

    # Split into parts around .where(...) segments
    remaining = expression
    segments: list[tuple] = []

    # Extract resource type
    dot = remaining.find(".")
    if dot < 1:
        return None
    resource_type = remaining[:dot]
    if not _re.match(r'^[A-Z][a-zA-Z]+$', resource_type):
        return None
    remaining = remaining[dot:]

    while remaining:
        # Find the next .where(
        where_match = _WHERE_SEGMENT_RE.search(remaining)
        if where_match is None:
            break

        # Path keys before this .where()
        pre = remaining[:where_match.start()]
        if pre:
            path_keys = tuple(k for k in pre.split(".") if k)
        else:
            path_keys = ()

        # Determine filter
        exact_val = where_match.group(1)
        prefix_val = where_match.group(2)
        if exact_val is not None:
            segments.append((path_keys, exact_val, "eq"))
        elif prefix_val is not None:
            segments.append((path_keys, prefix_val, "startsWith"))
        else:
            return None

        remaining = remaining[where_match.end():]

    if not segments:
        return None

    # Trailing path after the last .where()
    if remaining:
        trailing = tuple(k for k in remaining.split(".") if k)
    else:
        trailing = ()

    return (resource_type, tuple(segments), trailing)


def _evaluate_where_path(resource: dict, expression: str) -> list:
    """Evaluate a ``.where()``-containing expression via native dict traversal.

    Returns elements in the same ``[{"path": ..., "value": ...}]`` format
    that ``fhirpathpy + .log()`` produces, so callers see no difference.
    """
    plan = _parse_where_plan(expression)
    if plan is None:
        return []

    resource_type, segments, trailing = plan

    # Check resource type
    if not isinstance(resource, dict):
        return []
    if resource.get("resourceType") != resource_type:
        return []

    nodes = [resource]
    path_prefix = resource_type

    for path_keys, filter_value, filter_mode in segments:
        # Traverse dot-path to reach the array to filter
        for key in path_keys:
            path_prefix = f"{path_prefix}.{key}"
            next_nodes = []
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                child = node.get(key)
                if child is None:
                    continue
                if isinstance(child, list):
                    next_nodes.extend(child)
                else:
                    next_nodes.append(child)
            nodes = next_nodes
            if not nodes:
                return []

        # Apply .where(url=...) filter
        filtered = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            val = node.get("url")
            if filter_mode == "eq" and val == filter_value:
                filtered.append(node)
            elif filter_mode == "startsWith" and isinstance(val, str) and val.startswith(filter_value):
                filtered.append(node)
        nodes = filtered
        if not nodes:
            return []

    # Traverse trailing path keys
    for key in trailing:
        path_prefix = f"{path_prefix}.{key}"
        next_nodes = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            child = node.get(key)
            if child is None:
                continue
            if isinstance(child, list):
                next_nodes.extend(child)
            else:
                next_nodes.append(child)
        nodes = next_nodes
        if not nodes:
            return []

    # Return in fhirpathpy .log() format
    results = []
    for node in nodes:
        if isinstance(node, list):
            results.extend({"path": path_prefix, "value": item} for item in node)
        else:
            results.append({"path": path_prefix, "value": node})
    return results


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
        return _traverse(resource, parts[1:], parts[0])

    # For wildcard (*.field), use the actual resource type as the path prefix so
    # that elements produced here deduplicate correctly against typed rules.
    # Without this, "*.code.text" produces path "*.code.text" while the
    # candidate expansion also generates "Observation.code.text" from the same
    # rule, yielding two elements with different path strings that bypass the
    # (el_path, action) dedup check in action_dispatcher — causing actions
    # like nlp_scrub to run twice on the same field.
    return _traverse(resource, parts[1:], resource_type or "*")


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

@lru_cache(maxsize=512)
def _build_match_candidates_cached(match_expr: str, resource_type: str | None) -> tuple:
    """Expand match expression into concrete candidates (LRU-cached, thread-safe)."""
    if not isinstance(match_expr, str):
        return ()
    candidates = [match_expr]
    if resource_type:
        if match_expr.startswith("*."):
            candidates.append(f"{resource_type}{match_expr[1:]}")
        if "{resourceType}" in match_expr:
            candidates.append(match_expr.replace("{resourceType}", resource_type))
    return tuple(dict.fromkeys(candidates))


def _build_match_candidates(match_expr: str, resource: dict) -> tuple:
    """Expand a match expression into concrete FHIRPath candidates for this resource.

    Handles ``*.field`` wildcards and ``{resourceType}`` placeholders.
    """
    resource_type = resource.get("resourceType") if isinstance(resource, dict) else None
    return _build_match_candidates_cached(match_expr, resource_type)


# ---------------------------------------------------------------------------
# Rule index by ResourceType — fast lookup without scanning all rules
# ---------------------------------------------------------------------------

_rule_index_cache: dict = {}
_RULE_INDEX_CACHE_MAX = 64
_per_type_cache: dict[tuple, list] = {}
_PER_TYPE_CACHE_MAX = 256
_cache_lock = threading.Lock()


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
    rules_key = getattr(settings, "filename", None)

    # Rule index: lock-free read, lock only on miss (double-check)
    if rules_key and rules_key in _rule_index_cache:
        index = _rule_index_cache[rules_key]
    else:
        with _cache_lock:
            if rules_key and rules_key in _rule_index_cache:
                index = _rule_index_cache[rules_key]
            else:
                index = _build_rule_index(rules)
                if rules_key:
                    if len(_rule_index_cache) >= _RULE_INDEX_CACHE_MAX:
                        try:
                            oldest = next(iter(_rule_index_cache))
                            del _rule_index_cache[oldest]
                        except StopIteration:
                            pass
                    _rule_index_cache[rules_key] = index

    resource_type = resource.get("resourceType", "") if isinstance(resource, dict) else ""

    # Per-type cache: lock-free read, lock only on miss
    per_type_key = (rules_key, resource_type)
    cached_applicable = _per_type_cache.get(per_type_key)
    if cached_applicable is not None:
        return cached_applicable

    applicable = list(index.get(resource_type, []))
    applicable.extend(index.get("*", []))
    if rules_key:
        with _cache_lock:
            if len(_per_type_cache) >= _PER_TYPE_CACHE_MAX:
                # Evict oldest 25% instead of clearing the entire cache
                evict_count = _PER_TYPE_CACHE_MAX // 4
                for _k in list(_per_type_cache)[:evict_count]:
                    del _per_type_cache[_k]
            _per_type_cache[per_type_key] = applicable
    return applicable


def clear_rule_caches() -> None:
    """Clear all module-level rule caches (called when config profiles change)."""
    _compile_fhirpath.cache_clear()
    _classify_match.cache_clear()
    _build_match_candidates_cached.cache_clear()
    _parse_where_plan.cache_clear()
    with _cache_lock:
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
