"""Per-settings parse of the config rule list, memoized for the scoring hot path.

The privacy/quality evaluators derive the same settings-only facts (which paths
conditional rules cover, each rule's parsed match expression) on EVERY resource.
Within a batch the settings object is identical, so this single-slot memo turns
O(resources * rules) parsing into O(rules). On a settings-identity miss it
recomputes, so it stays correct even if two batches with different configs
interleave on the shared evaluator singletons.
"""

from __future__ import annotations

from dataclasses import dataclass

# Conditional actions only write a manifest entry when they actually transform
# something, so a clean field leaves no entry even though the rule ran. Treat
# their target paths as config-covered to avoid false identifier-risk positives.
_CONDITIONAL_ACTIONS = frozenset({"nlp_detect_act", "nlp_scrub", "nlp_detect"})


@dataclass(frozen=True)
class ParsedRules:
    """Settings-derived rule facts, computed once per settings object."""

    # Leaf paths covered by conditional (nlp_*) rules, e.g. "*.text" -> "text".
    config_covered_paths: frozenset[str]
    # (name, match_expr, root_field, n_segments) per rule - pre-split so the
    # per-resource coverage filters do not re-split match_expr on every resource.
    rules: tuple[tuple[str, str, str, int], ...]


def _leaf(match_expr: str) -> str:
    if match_expr.startswith("*."):
        return match_expr[2:]
    if "." in match_expr:
        return match_expr.split(".", 1)[1]
    return match_expr


_cache_settings: object | None = None
_cache_value: ParsedRules | None = None


def parsed_rules(settings) -> ParsedRules:
    """Return the memoized parse of ``settings.rules`` (keyed on settings identity)."""
    global _cache_settings, _cache_value
    if settings is _cache_settings and _cache_value is not None:
        return _cache_value

    covered: set[str] = set()
    parsed: list[tuple[str, str, str, int]] = []
    rules = getattr(settings, "rules", None) or []
    for rule in rules:
        match_expr = rule.get("match", "")
        name = rule.get("name", match_expr)
        parts = match_expr.split(".")
        root_field = parts[1] if len(parts) >= 2 else ""
        parsed.append((name, match_expr, root_field, len(parts)))
        if rule.get("action") in _CONDITIONAL_ACTIONS:
            covered.add(_leaf(match_expr))

    value = ParsedRules(frozenset(covered), tuple(parsed))
    _cache_settings = settings
    _cache_value = value
    return value
