"""FHIRPath rule matching and evaluation.

Owns the compiled-FHIRPath cache, the per-rules-set rule index, and the
parameter-interpolation helpers used by the action dispatch loop.
"""

from __future__ import annotations

import logging
import os
import re as _re
import threading
from copy import deepcopy
from functools import lru_cache

audit_log = logging.getLogger("medanon.audit")

# Cache sizes are env-tunable so operators can grow them when running with
# very large rule sets without a code change.  Defaults are sized to comfortably
# absorb every bundled profile (~50 unique paths) plus an order-of-magnitude
# headroom for custom profiles.
_FHIRPATH_CACHE_SIZE = int(os.environ.get("FHIRPATH_CACHE_SIZE", "512"))
_CLASSIFY_CACHE_SIZE = int(os.environ.get("FHIRPATH_CLASSIFY_CACHE_SIZE", "256"))
_WHERE_PLAN_CACHE_SIZE = int(os.environ.get("FHIRPATH_WHERE_CACHE_SIZE", "128"))
_CANDIDATES_CACHE_SIZE = int(os.environ.get("FHIRPATH_CANDIDATES_CACHE_SIZE", "512"))

# ---------------------------------------------------------------------------
# FHIRPath expression cache — compile once, reuse across resources
# ---------------------------------------------------------------------------

_fhirpathpy_log_registered = False
_fhirpathpy_log_lock = threading.Lock()


@lru_cache(maxsize=_FHIRPATH_CACHE_SIZE)
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
                    "fn": lambda ctx, els: [
                        {"path": x.path, "value": x.data} for x in els
                    ]
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
_SIMPLE_PATH_RE = _re.compile(r"^[A-Z][a-zA-Z]+(\.[a-zA-Z][a-zA-Z0-9]*)+$")
# Matches "*.meta.lastUpdated", "*.text.div" — wildcard dot-paths
_WILDCARD_PATH_RE = _re.compile(r"^\*(\.[a-zA-Z][a-zA-Z0-9]*)+$")


@lru_cache(maxsize=_CLASSIFY_CACHE_SIZE)
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
    r"url='([^']+)'"  # group 1: exact url value
    r"|"
    r"url\.startsWith\('([^']+)'\)"  # group 2: prefix value
    r")"
    r"\)"
)


@lru_cache(maxsize=_WHERE_PLAN_CACHE_SIZE)
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
    if not _re.match(r"^[A-Z][a-zA-Z]+$", resource_type):
        return None
    remaining = remaining[dot:]

    while remaining:
        # Find the next .where(
        where_match = _WHERE_SEGMENT_RE.search(remaining)
        if where_match is None:
            break

        # Path keys before this .where()
        pre = remaining[: where_match.start()]
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

        remaining = remaining[where_match.end() :]

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
            elif (
                filter_mode == "startsWith"
                and isinstance(val, str)
                and val.startswith(filter_value)
            ):
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


@lru_cache(maxsize=_CANDIDATES_CACHE_SIZE)
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
# Cache observability — sampled on /metrics scrape
# ---------------------------------------------------------------------------

_CACHED_FUNCS = {
    "compile": _compile_fhirpath,
    "classify": _classify_match,
    "where_plan": _parse_where_plan,
    "candidates": _build_match_candidates_cached,
}


def sample_cache_metrics() -> None:
    """Push current ``cache_info()`` of every FHIRPath LRU cache to Prometheus.

    Called from the ``/metrics`` endpoint so the cost is paid only at scrape
    time (not per request).  Safe to call without prometheus_client installed.
    """
    try:
        from utils.metrics import (
            FHIRPATH_CACHE_HITS,
            FHIRPATH_CACHE_MAXSIZE,
            FHIRPATH_CACHE_MISSES,
            FHIRPATH_CACHE_SIZE,
        )
    except Exception:  # pragma: no cover - metrics optional
        return
    for name, fn in _CACHED_FUNCS.items():
        info = fn.cache_info()
        FHIRPATH_CACHE_HITS.labels(cache=name).set(info.hits)
        FHIRPATH_CACHE_MISSES.labels(cache=name).set(info.misses)
        FHIRPATH_CACHE_SIZE.labels(cache=name).set(info.currsize)
        FHIRPATH_CACHE_MAXSIZE.labels(cache=name).set(info.maxsize or 0)


# ---------------------------------------------------------------------------
# Rule index by ResourceType — fast lookup without scanning all rules
# ---------------------------------------------------------------------------

_rule_index_cache: dict = {}
_RULE_INDEX_CACHE_MAX = 64
_per_type_cache: dict[tuple, list] = {}
_PER_TYPE_CACHE_MAX = 256
_cache_lock = threading.Lock()


def _settings_cache_key(settings) -> "tuple | None":
    """Content-dependent cache key for per-profile caches.

    ``(filename, config_hash)`` — including the content hash makes
    stale-after-edit structurally impossible: an in-place profile edit
    produces a new hash, so old cache entries are simply never hit again
    (they age out of the bounded caches).
    """
    filename = getattr(settings, "filename", None)
    if filename is None:
        return None
    return (filename, getattr(settings, "config_hash", None))


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


_DEFAULT_PRIORITY = 100


def _build_rule_index(rules: list) -> dict[str, list]:
    """Build ``{resourceType: [rules]}`` index for fast rule lookup.

    Rules are stable-sorted by ``priority`` (ascending; lower number = higher
    priority, same as many scheduler conventions) so that dispatch always fires
    higher-priority rules first.  Rules without a ``priority`` key default to
    ``_DEFAULT_PRIORITY`` (100); within the same priority value the YAML order
    is preserved (stable sort).
    """
    index: dict[str, list] = {}
    for rule in rules:
        if not isinstance(rule, dict) or "match" not in rule or "action" not in rule:
            continue
        rt = _get_rule_resource_type(rule)
        index.setdefault(rt, []).append(rule)
    for rt, rule_list in index.items():
        index[rt] = sorted(
            rule_list,
            key=lambda r: int(r.get("priority", _DEFAULT_PRIORITY)),
        )
    return index


def _get_rules_for_resource(resource: dict, settings) -> list:
    """Return only the rules applicable to this resource's type."""
    rules = getattr(settings, "rules", [])
    rules_key = _settings_cache_key(settings)

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

    resource_type = (
        resource.get("resourceType", "") if isinstance(resource, dict) else ""
    )

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


def warm_rule_caches(settings) -> int:
    """Pre-populate FHIRPath caches for every rule in *settings*.

    Iterates all match expressions, classifies them, and (for "complex" / full
    FHIRPath expressions) compiles the ANTLR grammar eagerly so the first real
    request does not pay the compile cost.

    Returns the number of expressions compiled.
    """
    import time

    rules = getattr(settings, "rules", [])
    compiled = 0
    t0 = time.monotonic()
    for rule in rules:
        expr = rule.get("match", "") if isinstance(rule, dict) else ""
        if not expr:
            continue
        kind = _classify_match(expr)
        if kind == "complex":
            try:
                _compile_fhirpath(expr)
                compiled += 1
            except Exception:
                pass
    elapsed_ms = (time.monotonic() - t0) * 1000
    _log = __import__("logging").getLogger("medanon.rule_matcher")
    _log.debug("fhirpath_warmup rules=%d compiled=%d elapsed_ms=%.1f", len(rules), compiled, elapsed_ms)
    return compiled


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


_NLP_ACTIONS = frozenset({"nlp_scrub", "nlp_detect", "nlp_detect_act"})


def _merge_profile_nlp(params: dict, rule: dict, settings) -> dict:
    """Inject profile-level ``nlp.entity_actions`` / ``entity_priorities`` into
    NLP-action params, with rule-level params taking precedence.

    Precedence (lowest → highest): hard-coded defaults (applied in the action)
    < profile ``nlp:`` block < per-rule ``params``.  This lets an operator set
    the entity→action policy once in the profile YAML instead of repeating it on
    every rule or editing code.  No-op for non-NLP actions and when no profile
    ``nlp`` block is present.
    """
    if rule.get("action") not in _NLP_ACTIONS:
        return params
    nlp_cfg = getattr(settings, "nlp", None)
    if not isinstance(nlp_cfg, dict):
        return params

    profile_actions = nlp_cfg.get("entity_actions")
    profile_priorities = nlp_cfg.get("entity_priorities")
    profile_fail_mode = nlp_cfg.get("fail_mode")
    if (
        not isinstance(profile_actions, dict)
        and not isinstance(profile_priorities, dict)
        and profile_fail_mode is None
    ):
        return params

    merged = dict(params)  # shallow copy so we never mutate the rule's params
    if isinstance(profile_actions, dict):
        rule_actions = merged.get("entity_actions") or {}
        # profile is the base; rule-level entries override.
        merged["entity_actions"] = {**profile_actions, **rule_actions}
    if isinstance(profile_priorities, dict):
        rule_priorities = merged.get("entity_priorities") or {}
        merged["entity_priorities"] = {**profile_priorities, **rule_priorities}
    # fail_mode: profile is the base; a per-rule param overrides it (E3.3).
    if profile_fail_mode is not None and "fail_mode" not in merged:
        merged["fail_mode"] = profile_fail_mode
    return merged


def _resolve_rule_params(rule: dict, settings) -> dict:
    """Return rule params merged with any dynamic overrides from *settings*."""
    dynamic_settings = getattr(settings, "dynamic_rule_settings", None)
    # Skip deepcopy when no dynamic settings (the common case)
    if not isinstance(dynamic_settings, dict) or not dynamic_settings:
        return _merge_profile_nlp(rule.get("params", {}), rule, settings)

    params = deepcopy(rule["params"]) if "params" in rule else {}
    params = _interpolate_dynamic(params, dynamic_settings)
    for key, value in dynamic_settings.items():
        if key in params:
            params[key] = value
    return _merge_profile_nlp(params, rule, settings)


# ---------------------------------------------------------------------------
# Conditional rule evaluation (E2.4)
# ---------------------------------------------------------------------------

_CONDITION_OPS = frozenset({"eq", "ne", "in", "not_in", "exists", "not_exists"})


def _condition_values(resource: dict, path: str) -> list:
    """Collect the value(s) a condition ``path`` resolves to in *resource*.

    Reuses the same FHIRPath fast-paths as rule matching so a condition can
    target any expression a ``match`` can (simple dot-paths, wildcards,
    ``.where(...)``, or full FHIRPath).  Evaluation errors degrade to an empty
    result rather than raising — a malformed condition must never crash the
    pipeline.
    """
    values: list = []
    for candidate in _build_match_candidates(path, resource):
        match_class = _classify_match(candidate)
        try:
            if match_class in ("simple", "wildcard"):
                nodes = _evaluate_simple_path(resource, candidate)
            elif match_class == "where":
                nodes = _evaluate_where_path(resource, candidate)
            else:
                nodes = _evaluate_fhirpath_cached(resource, candidate)
        except Exception:
            nodes = []
        for node in nodes:
            values.append(node.get("value") if isinstance(node, dict) else node)
    return values


def _eval_single_condition(resource: dict, cond: dict) -> bool:
    """Evaluate one ``{path, op, value}`` condition against *resource*."""
    path = cond.get("path")
    if not path:
        return True
    op = str(cond.get("op", "exists")).strip().lower()
    if op not in _CONDITION_OPS:
        audit_log.warning(
            "rule_condition_invalid_op op=%r path=%s — treating rule as applicable",
            op,
            path,
        )
        return True

    values = _condition_values(resource, path)

    if op == "exists":
        return len(values) > 0
    if op == "not_exists":
        return len(values) == 0

    expected = cond.get("value")
    if op == "eq":
        return any(v == expected for v in values)
    if op == "ne":
        return not any(v == expected for v in values)

    options = expected if isinstance(expected, (list, tuple, set)) else [expected]
    if op == "in":
        return any(v in options for v in values)
    if op == "not_in":
        return not any(v in options for v in values)
    return True


def _eval_condition_node(resource: dict, node: dict) -> bool:
    """Evaluate a single condition node, honouring ``any_of`` / ``all_of``."""
    if not isinstance(node, dict):
        return True
    if isinstance(node.get("any_of"), list):
        return any(_eval_condition_node(resource, c) for c in node["any_of"])
    if isinstance(node.get("all_of"), list):
        return all(_eval_condition_node(resource, c) for c in node["all_of"])
    return _eval_single_condition(resource, node)


def evaluate_rule_condition(rule: dict, resource: dict) -> bool:
    """Return ``True`` if *rule* should fire against *resource* (E2.4).

    A rule may carry an optional ``condition`` (single node) and/or a
    ``conditions`` list (all must pass — logical AND).  Each node supports the
    operators ``eq``/``ne``/``in``/``not_in``/``exists``/``not_exists`` and the
    nesting keys ``any_of`` (OR) / ``all_of`` (AND).  Rules without either key
    always fire, so existing profiles are unaffected.
    """
    cond = rule.get("condition")
    conds = rule.get("conditions")
    if cond is None and conds is None:
        return True
    if not isinstance(resource, dict):
        return True

    if isinstance(cond, dict) and not _eval_condition_node(resource, cond):
        return False
    if isinstance(conds, list):
        for node in conds:
            if not _eval_condition_node(resource, node):
                return False
    return True


# ---------------------------------------------------------------------------
# Static rule-conflict detection
# ---------------------------------------------------------------------------


def detect_rule_conflicts(rules: list[dict]) -> list[dict]:
    """Return conflicts where multiple rules target the same match path with a
    *different* action.

    A conflict means the same FHIR path is claimed by more than one action
    (e.g. one rule ``redact``s ``Patient.telecom.value`` while another ``mask``s
    it).  The order rules fire in then decides the outcome, which is usually a
    config mistake.  Two rules with the *same* action on the same path are not
    reported — that is a harmless duplicate.

    Each returned conflict is a dict::

        {"path": <match>, "actions": [<action_a>, <action_b>, ...],
         "rules": [<rule_name_a>, <rule_name_b>, ...]}
    """
    by_path: dict[str, list[tuple[str, str]]] = {}
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = rule.get("match")
        action = rule.get("action")
        if not isinstance(match, str) or not isinstance(action, str):
            continue
        name = str(rule.get("name", match))
        by_path.setdefault(match, []).append((action, name))

    conflicts: list[dict] = []
    for path, entries in by_path.items():
        actions = {action for action, _ in entries}
        if len(actions) > 1:
            conflicts.append(
                {
                    "path": path,
                    "actions": sorted(actions),
                    "rules": [name for _, name in entries],
                }
            )
    return conflicts
