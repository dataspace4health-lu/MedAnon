"""Per-element action dispatch loop (Pass 1).

Evaluates FHIRPath matches for each rule, deduplicates across overlapping rules,
dispatches to de-identification / pseudonymization actions immediately, and
accumulates gPAS work items for the batch Pass 2.
"""

from __future__ import annotations

import logging
from utils.json_fast import dumps as _json_dumps, dumps_sorted as _json_dumps_sorted
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
    evaluate_rule_condition,
)
from pipeline.deidentify import (
    deident_actions as _deident_actions,
    pseudo_actions as _pseudo_actions,
    depseudo_actions as _depseudo_actions,
    perform_deidentification,
    perform_pseudonymization,
    perform_depseudonymization,
)
from pipeline.exceptions import FallbackRedactError
from pipeline.structural_phi import (
    apply_structural_heuristics as _apply_structural_heuristics,
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
class PseudonymizationTask:
    """A single FHIR element deferred for batch gPAS identifier pseudonymization."""

    rule: dict
    element: dict  # FHIRPath node with ``path`` and ``value`` keys
    params: dict
    serialized_value: str = field(default="", repr=False)


@dataclass
class PHIDetectionTask:
    """A single FHIR text field deferred for batch NLP PHI detection."""

    rule: dict
    element: dict  # FHIRPath node with ``path`` and ``value`` keys
    params: dict
    action_type: str  # 'nlp_scrub', 'nlp_detect', or 'nlp_detect_act'


# Backward-compatible aliases — remove after all callers are updated.
BatchWork = PseudonymizationTask
NlpWork = PHIDetectionTask


def _bare_path_prefix(expr: str) -> str:
    """Trim a FHIRPath expression to its leading bare dot-path.

    ``"Patient.name.where(use='official').given"`` → ``"Patient.name"``.
    ``find_nodes`` silently returns ``[]`` for function segments like
    ``where(...)``, so a fallback redact targeting the raw expression would
    no-op and leave the field intact. Redacting the longest plain-navigable
    prefix over-redacts (drops the whole parent field) — the fail-closed
    direction.
    """
    expr = expr.strip()
    paren = expr.find("(")
    if paren != -1:
        head = expr[:paren]
        expr = head.rsplit(".", 1)[0] if "." in head else head
    return expr.strip().rstrip(".")


# ---------------------------------------------------------------------------
# Rule evaluation: evaluate de-identification rules, dispatch immediate actions,
# collect deferred pseudonymization and PHI-detection tasks.
# ---------------------------------------------------------------------------


def evaluate_and_dispatch(
    resource: dict,
    applicable_rules: list,
    settings,
    manifest_entries: list,
    processing_mode: str,
) -> tuple[list[PseudonymizationTask], list[PHIDetectionTask]]:
    """Evaluate all de-identification rules against *resource*.

    Applies non-gPAS/NLP actions immediately (in place). Defers identifier
    pseudonymization and PHI detection to their respective batch passes.

    Returns:
        ``(pseudonymization_tasks, phi_detection_tasks)`` — work items for
        the pseudonymization and PHI-detection stages respectively.
    """
    gpas_work: list[PseudonymizationTask] = []
    nlp_work: list[PHIDetectionTask] = []
    # Dedup key: (path, action, stable_value_key) — see _stable_value_key below.
    processed_paths: set[tuple] = set()
    # Conflict detection: first action applied to each concrete path. A later
    # rule with a *different* action on the same path is a config conflict.
    path_action_seen: dict[str, str] = {}

    _apply_structural_heuristics(resource)

    for rule in applicable_rules:
        action = rule["action"]
        params = _resolve_rule_params(rule, settings)

        # Conditional rules (E2.4): skip this rule entirely when its optional
        # condition/conditions block does not hold for the current resource.
        if not evaluate_rule_condition(rule, resource):
            audit_log.debug(
                "rule_skipped_condition action=%s match=%s",
                action,
                rule.get("match"),
            )
            if _MANIFEST_ENABLED:
                manifest_entries.append(
                    {
                        "rule": rule.get("name", rule["match"]),
                        "action": action,
                        "path": rule.get("match", ""),
                        "skipped_reason": "condition_not_met",
                    }
                )
            continue

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
                        # Fail-safe: redact the longest plain-navigable prefix of
                        # the candidate path. The raw expression may contain
                        # function segments (where(), first(), …) that find_nodes
                        # cannot navigate — it returns [] for those, so a redact
                        # targeting the raw expression silently no-ops and the
                        # element passes through unprocessed.
                        fallback_path = _bare_path_prefix(candidate)
                        fallback_el = {"path": fallback_path, "value": None}
                        try:
                            from utils.metrics import ACTION_FALLBACK

                            ACTION_FALLBACK.labels(
                                action=str(action), reason=type(exc).__name__
                            ).inc()
                        except Exception:  # noqa: BLE001 — metrics must never break the pipeline
                            pass
                        try:
                            perform_deidentification(
                                "redact", resource, fallback_el, {}
                            )
                        except Exception as redact_exc:
                            # A resource we can neither evaluate nor redact must
                            # not be emitted: raise so the skip-mode handler in
                            # the processor quarantines the whole resource.
                            audit_log.error(
                                "fhirpath_fallback_redact_failed expression=%s "
                                "fallback_path=%s error_type=%s — quarantining resource",
                                candidate,
                                fallback_path,
                                type(redact_exc).__name__,
                            )
                            raise FallbackRedactError(
                                f"fallback redact failed for path {fallback_path!r} "
                                f"after FHIRPath evaluation error"
                            ) from redact_exc
                        if _MANIFEST_ENABLED:
                            manifest_entries.append(
                                {
                                    "rule": rule.get("name", rule["match"]),
                                    "action": "redact",
                                    "path": fallback_path,
                                    "reason": "fhirpath_eval_fallback",
                                }
                            )
                        continue
                    raise

        # Filter elements already processed by a prior rule with the same action.
        # Dedup key uses a *stable, value-based* identity so output is
        # deterministic across runs and threads.  Previously this used id() of
        # mutable matched values, which is only unique within an object's
        # lifetime and can be reused after GC — under parallel processing that
        # produced non-reproducible output (a privacy-tool disqualifier).
        def _stable_value_key(v):
            if v is None or isinstance(v, (str, int, float, bool)):
                return v
            try:
                # Sorted keys make the digest stable across runs / Python
                # processes (orjson preserves insertion order otherwise).
                return _json_dumps_sorted(v)
            except Exception:
                # Last-resort fallback — identity is OK here because the only
                # path that hits this branch is non-JSON-serializable objects,
                # which never come from FHIR resources in practice.
                return f"unhashable:{id(v)}"

        # Include a param fingerprint in the dedup key so two rules with the
        # same action but different params on the same path/value are both
        # applied.  Without this, a stricter follow-on rule (e.g. a second
        # generalize with a different strategy) is silently dropped, which is
        # a quiet privacy regression.
        # Use _stable_value_key on sorted params items for a cheap stable digest.
        try:
            params_key = _stable_value_key(
                {k: params[k] for k in sorted(params) if not k.startswith("_")}
            )
        except Exception:
            params_key = repr(sorted(params.items()))

        elements_to_process = []
        for el in matched_elements:
            el_path = el.get("path", "?")
            el_val = el.get("value")
            val_key = _stable_value_key(el_val)
            path_key = (el_path, action, val_key, params_key)
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
            val_key = _stable_value_key(el_val)
            processed_paths.add((el.get("path", "?"), action, val_key, params_key))
            # Surface config conflicts: same path claimed by a different action.
            el_path = el.get("path", "?")
            prior = path_action_seen.get(el_path)
            if prior is None:
                path_action_seen[el_path] = action
            elif prior != action:
                audit_log.warning(
                    "rule_conflict path=%s action_a=%s action_b=%s",
                    el_path,
                    prior,
                    action,
                )
                try:
                    from utils.metrics import RULE_CONFLICT_TOTAL

                    RULE_CONFLICT_TOTAL.labels(
                        path=el_path, action_a=prior, action_b=action
                    ).inc()
                except Exception:  # noqa: BLE001 — metrics must never break the pipeline
                    pass

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
                    serialized = val[len("urn:uuid:") :]
                gpas_work.append(
                    PseudonymizationTask(
                        rule=rule,
                        element=el,
                        params=el_params,
                        serialized_value=serialized,
                    )
                )
                # Manifest is recorded at write-back time (run_gpas_batch /
                # apply_pseudonym_mapping) so the logged action reflects the actual
                # outcome — pseudonymization or fallback-redact if gPAS failed.
                continue

            # Defer NLP actions for batch processing (Pass 1.5)
            if action in NLP_BATCH_ACTIONS:
                nlp_work.append(
                    PHIDetectionTask(
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
                        from utils.metrics import ACTION_FALLBACK

                        ACTION_FALLBACK.labels(
                            action=str(action), reason=type(exc).__name__
                        ).inc()
                    except Exception:
                        pass
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


# Backward-compatible alias for evaluate_and_dispatch.
dispatch_pass1 = evaluate_and_dispatch
