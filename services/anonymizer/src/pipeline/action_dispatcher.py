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
GPAS_DEPSEUDO_ACTIONS = frozenset({"gpas_depseudonymize"})
NLP_BATCH_ACTIONS = frozenset({"nlp_scrub", "nlp_detect", "nlp_detect_act"})


# ---------------------------------------------------------------------------
# Work items for deferred batch calls
# ---------------------------------------------------------------------------


@dataclass
class BatchWork:
    """A single element deferred to the gPAS batch pass."""

    rule: dict
    element: dict  # FHIRPath node with ``path`` and ``value`` keys
    params: dict
    serialized_value: str = field(default="", repr=False)


@dataclass
class NlpWork:
    """A single element deferred to the NLP batch pass."""

    rule: dict
    element: dict  # FHIRPath node with ``path`` and ``value`` keys
    params: dict
    action_type: str  # 'nlp_scrub', 'nlp_detect', or 'nlp_detect_act'


# ---------------------------------------------------------------------------
# Pass 1: evaluate rules, dispatch non-gPAS actions, collect gPAS work
# ---------------------------------------------------------------------------


def dispatch_pass1(
    resource: dict,
    applicable_rules: list,
    settings,
    manifest_entries: list,
    processing_mode: str,
) -> tuple[list[BatchWork], list[NlpWork]]:
    """Evaluate all rules against *resource*, applying non-gPAS/NLP actions immediately.

    Modifies *resource* in place.  Appends fired-rule metadata to
    *manifest_entries* when the manifest is enabled.

    Returns:
        Tuple of (gPAS :class:`BatchWork` items, NLP :class:`NlpWork` items)
        for the respective batch passes.
    """
    gpas_work: list[BatchWork] = []
    nlp_work: list[NlpWork] = []
    processed_paths: set[tuple[str, str]] = set()

    for rule in applicable_rules:
        action = rule["action"]
        params = _resolve_rule_params(rule, settings)

        # Apply domain_map override: route this resource type to its leaf gPAS domain.
        # Only runs for gpas_pseudonymize/gpas_depseudonymize when the config has a domain_map.
        if action in GPAS_PSEUDO_ACTIONS or action in GPAS_DEPSEUDO_ACTIONS:
            _domain_map = getattr(settings, "domain_map", None)
            if _domain_map:
                resource_type = (
                    resource.get("resourceType") if isinstance(resource, dict) else None
                )
                if resource_type and resource_type in _domain_map:
                    params = dict(params)  # shallow copy — gpas_domain is a scalar
                    params["gpas_domain"] = _domain_map[resource_type]

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
                except Exception as exc:
                    if processing_mode == "skip":
                        audit_log.warning(
                            "fhirpath_eval_failed_skip expression=%s resource_type=%s error=%s — "
                            "redacting matched path as safety fallback",
                            candidate.replace("\n", " ").replace("\r", " "),
                            resource.get("resourceType", "unknown")
                            if isinstance(resource, dict)
                            else "unknown",
                            type(exc).__name__,
                        )
                        # Fail-safe: construct a synthetic element targeting the
                        # candidate path so the downstream redact action can strip
                        # the field.  Without this, a FHIRPath evaluation failure
                        # silently passes the element through unprocessed.
                        fallback_el = {"path": candidate, "value": None}
                        try:
                            perform_deidentification(
                                "redact", resource, fallback_el, {}
                            )
                        except Exception:
                            audit_log.error(
                                "fhirpath_fallback_redact_failed expression=%s — PHI may be exposed",
                                candidate,
                            )
                        continue
                    raise

        # Filter elements already processed by a prior rule with the same action.
        # The key includes value identity so that different array elements at the
        # same structural path (e.g. race vs ethnicity sub-extensions both at
        # Patient.extension.extension.valueString) are NOT treated as duplicates.
        elements_to_process = []
        for el in matched_elements:
            el_path = el.get("path", "?")
            el_val = el.get("value")
            # id() is safe here: el_val is a live reference held in matched_elements,
            # so the object cannot be GC'd and its id() cannot be reused within this
            # tight loop.  Scalar values (str/int/bool) use the value itself as key.
            val_id = id(el_val) if isinstance(el_val, (dict, list)) else el_val
            path_key = (el_path, action, val_id)
            if path_key in processed_paths:
                audit_log.debug(
                    "rule_skipped_duplicate action=%s path=%s",
                    action,
                    el_path,
                )
                continue
            elements_to_process.append(el)

        for el in elements_to_process:
            el_val = el.get("value")
            val_id = id(el_val) if isinstance(el_val, (dict, list)) else el_val
            processed_paths.add((el.get("path", "?"), action, val_id))

        for el in elements_to_process:
            el_path = el.get("path", "?")
            # Copy params per-element to prevent mutations (_no_change,
            # _actual_action) from leaking between elements sharing the same rule.
            el_params = dict(params)

            audit_log.debug(
                "rule_applied action=%s match=%s path=%s resource_type=%s",
                action,
                rule["match"],
                el_path,
                resource.get("resourceType", "unknown")
                if isinstance(resource, dict)
                else "unknown",
            )

            if action in GPAS_PSEUDO_ACTIONS or action in GPAS_DEPSEUDO_ACTIONS:
                val = el["value"]
                serialized = str(val) if not isinstance(val, dict) else _json_dumps(val)
                # Normalize urn:uuid: so the same bare UUID always maps to the same
                # pseudonym regardless of reference format. Without this, a resource id
                # "abc-123" (bare) and a reference "urn:uuid:abc-123" produce two
                # separate gPAS entries with different pseudonyms, breaking linkage.
                if isinstance(val, str) and val.startswith("urn:uuid:"):
                    serialized = val[len("urn:uuid:"):]
                gpas_work.append(
                    BatchWork(
                        rule=rule,
                        element=el,
                        params=el_params,
                        serialized_value=serialized,
                    )
                )
                # Manifest is recorded at write-back time (run_gpas_batch /
                # write_back_gpas_batch) so the logged action reflects the actual
                # outcome — pseudonymization or fallback-redact if gPAS failed.
                continue

            # Defer NLP actions for batch processing (Pass 1.5)
            if action in NLP_BATCH_ACTIONS:
                nlp_work.append(
                    NlpWork(
                        rule=rule,
                        element=el,
                        params=el_params,
                        action_type=action,
                    )
                )
                continue

            actual_action = action
            try:
                if action in DEIDENT_ACTIONS:
                    perform_deidentification(action, resource, el, el_params)
                elif action in PSEUDO_ACTIONS:
                    perform_pseudonymization(action, resource, el, el_params)
                elif action in DEPSEUDO_ACTIONS:
                    perform_depseudonymization(action, resource, el, el_params)
                else:
                    not_implemented(f"Method {action} is not implemented")
            except Exception as exc:
                if processing_mode == "skip":
                    audit_log.warning(
                        "rule_failed_skip action=%s match=%s path=%s error_type=%s",
                        action,
                        rule.get("match"),
                        el_path,
                        type(exc).__name__,
                        exc_info=False,
                    )
                    try:
                        perform_deidentification("redact", resource, el, {})
                        actual_action = "redact"
                    except Exception:
                        audit_log.error(
                            "fallback_redact_failed path=%s — PHI may be exposed; re-raising",
                            el_path,
                        )
                        raise
                    # Record the fallback action in manifest, then continue
                    if _MANIFEST_ENABLED:
                        manifest_entries.append(
                            {
                                "rule": rule.get("name", rule["match"]),
                                "action": actual_action,
                                "path": el_path,
                            }
                        )
                    continue
                raise

            # Record manifest after successful action execution
            if _MANIFEST_ENABLED:
                # Conditional-manifest: nlp_detect_act skips manifest when
                # NLP found nothing (text unchanged, no info loss to record).
                if el_params.get("_no_change"):
                    continue
                # Use the actual sub-action when the action reports one
                # (e.g. "nlp_detect_act/redact" for targeted replacements).
                reported_action = el_params.get("_actual_action", actual_action)
                manifest_entries.append(
                    {
                        "rule": rule.get("name", rule["match"]),
                        "action": reported_action,
                        "path": el_path,
                    }
                )

    return gpas_work, nlp_work
