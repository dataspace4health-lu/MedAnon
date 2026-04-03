"""Per-element action dispatch loop (Pass 1).

Evaluates FHIRPath matches for each rule, deduplicates across overlapping rules,
dispatches to de-identification / pseudonymization actions immediately, and
accumulates gPAS work items for the batch Pass 2.
"""

from __future__ import annotations

import logging
from utils.json_fast import dumps as _json_dumps
from dataclasses import dataclass, field

from utils.fhirpath import not_implemented
from pipeline.manifest import _MANIFEST_ENABLED
from pipeline.rule_matcher import (
    _build_match_candidates,
    _classify_match,
    _evaluate_fhirpath_cached,
    _evaluate_simple_path,
    _evaluate_where_path,
    _resolve_rule_params,
)
from pipeline.deidentify import (
    deident_actions as _deident_actions,
    pseudo_actions as _pseudo_actions,
    depseudo_actions as _depseudo_actions,
    perform_deidentification,
    perform_pseudonymization,
    perform_depseudonymization,
)

audit_log = logging.getLogger("medanon.audit")

# ---------------------------------------------------------------------------
# Action category sets
# ---------------------------------------------------------------------------

DEIDENT_ACTIONS = frozenset(_deident_actions)
PSEUDO_ACTIONS = frozenset(_pseudo_actions)
DEPSEUDO_ACTIONS = frozenset(_depseudo_actions)
GPAS_PSEUDO_ACTIONS = frozenset({"gpas_pseudonymize"})


# ---------------------------------------------------------------------------
# Work item for deferred gPAS batch call
# ---------------------------------------------------------------------------

@dataclass
class BatchWork:
    """A single element deferred to the gPAS batch pass."""
    rule: dict
    element: dict   # FHIRPath node with ``path`` and ``value`` keys
    params: dict
    serialized_value: str = field(default="", repr=False)


# ---------------------------------------------------------------------------
# Pass 1: evaluate rules, dispatch non-gPAS actions, collect gPAS work
# ---------------------------------------------------------------------------

def dispatch_pass1(
    resource: dict,
    applicable_rules: list,
    settings,
    manifest_entries: list,
    processing_mode: str,
) -> list[BatchWork]:
    """Evaluate all rules against *resource*, applying non-gPAS actions immediately.

    Modifies *resource* in place.  Appends fired-rule metadata to
    *manifest_entries* when the manifest is enabled.

    Returns:
        List of :class:`BatchWork` items for the gPAS batch pass (Pass 2).
    """
    gpas_work: list[BatchWork] = []
    processed_paths: set[tuple[str, str]] = set()

    for rule in applicable_rules:
        action = rule["action"]
        params = _resolve_rule_params(rule, settings)

        # Evaluate FHIRPath match candidates, collecting node elements
        matched_elements: list[dict] = []
        for candidate in _build_match_candidates(rule["match"], resource):
            match_class = _classify_match(candidate)
            if match_class in ("simple", "wildcard"):
                matched_elements.extend(_evaluate_simple_path(resource, candidate))
            elif match_class == "where":
                matched_elements.extend(_evaluate_where_path(resource, candidate))
            else:
                try:
                    matched = _evaluate_fhirpath_cached(resource, candidate + ".log()")
                    matched_elements.extend(matched)
                except Exception:
                    audit_log.debug(
                        "fhirpath_eval_failed expression=%s resource_type=%s",
                        candidate.replace("\n", " ").replace("\r", " "),
                        resource.get("resourceType", "unknown") if isinstance(resource, dict) else "unknown",
                    )
                    continue

        # Determine action category for duplicate-path filtering
        if action in DEIDENT_ACTIONS:
            category = "deidentify"
        elif action in PSEUDO_ACTIONS:
            category = "pseudonymize"
        elif action in DEPSEUDO_ACTIONS:
            category = "depseudonymize"
        else:
            category = "unknown"

        # Filter elements already processed by a prior rule in the same category
        elements_to_process = []
        for el in matched_elements:
            el_path = el.get("path", "?")
            path_key = (el_path, category)
            if path_key in processed_paths:
                audit_log.debug(
                    "rule_skipped_duplicate action=%s path=%s category=%s",
                    action, el_path, category,
                )
                continue
            elements_to_process.append(el)

        for el in elements_to_process:
            processed_paths.add((el.get("path", "?"), category))

        for el in elements_to_process:
            el_path = el.get("path", "?")

            audit_log.debug(
                "rule_applied action=%s match=%s path=%s resource_type=%s",
                action,
                rule["match"],
                el_path,
                resource.get("resourceType", "unknown") if isinstance(resource, dict) else "unknown",
            )

            if _MANIFEST_ENABLED:
                manifest_entries.append({
                    "rule": rule.get("name", rule["match"]),
                    "action": action,
                    "path": el_path,
                })

            if action in GPAS_PSEUDO_ACTIONS:
                val = el["value"]
                serialized = str(val) if not isinstance(val, dict) else _json_dumps(val)
                gpas_work.append(BatchWork(
                    rule=rule, element=el, params=params,
                    serialized_value=serialized,
                ))
                continue

            try:
                if action in DEIDENT_ACTIONS:
                    perform_deidentification(action, resource, el, params)
                elif action in PSEUDO_ACTIONS:
                    perform_pseudonymization(action, resource, el, params)
                elif action in DEPSEUDO_ACTIONS:
                    perform_depseudonymization(action, resource, el, params)
                else:
                    not_implemented(f"Method {action} is not implemented")
            except Exception as exc:
                if processing_mode == "skip":
                    audit_log.warning(
                        "rule_failed_skip action=%s match=%s path=%s error_type=%s",
                        action, rule.get("match"), el_path, type(exc).__name__,
                        exc_info=False,
                    )
                    try:
                        perform_deidentification("redact", resource, el, {})
                    except Exception:
                        audit_log.error(
                            "fallback_redact_failed path=%s — PHI may be exposed; re-raising",
                            el_path,
                        )
                        raise
                    continue
                raise

    return gpas_work
