"""Pass 2: batch gPAS pseudonymization.

Collects the original values from all deferred BatchWork items, calls
the pseudonymizer in a single batch, then writes pseudonyms back into
the resource via the same node-substitution path used by the non-batch
actions.
"""

from __future__ import annotations

import json
import logging

from utils.fhirpath import find_nodes
from actions.substitute import _substitute_nodes
from pipeline.action_dispatcher import BatchWork
from pipeline.rule_matcher import _resolve_rule_params
from pipeline.deidentify import perform_deidentification
from integrations.gpas.circuit_breaker import GpasUnavailableError  # noqa: F401 — re-exported

audit_log = logging.getLogger("medanon.audit")

# ---------------------------------------------------------------------------
# gPAS params cache — avoid re-scanning rules on every resource
# ---------------------------------------------------------------------------

_gpas_params_cache: dict = {}


def _extract_gpas_params(settings) -> dict | None:
    """Return the params dict from the first ``gpas_pseudonymize`` rule, or *None*."""
    rules = getattr(settings, "rules", [])
    rules_id = id(rules)
    if rules_id in _gpas_params_cache:
        return _gpas_params_cache[rules_id]
    result = None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("action") == "gpas_pseudonymize":
            result = _resolve_rule_params(rule, settings)
            break
    _gpas_params_cache[rules_id] = result
    return result


# ---------------------------------------------------------------------------
# Pass 2 batch runner
# ---------------------------------------------------------------------------

def run_gpas_batch(
    resource: dict,
    gpas_work: list[BatchWork],
    processing_mode: str,
    pseudonymizer,
) -> dict:
    """Batch-pseudonymize all deferred gPAS work items.

    Calls ``pseudonymizer.pseudonymize_batch()`` once for all values,
    then writes each pseudonym back into *resource* in place.

    Args:
        resource:        The resource being processed (mutated in place).
        gpas_work:       Deferred work items from Pass 1.
        processing_mode: ``'raise'`` or ``'skip'`` on error.
        pseudonymizer:   :class:`~pipeline.ports.PseudonymizerPort` implementation.

    Returns:
        The ``{original: pseudonym}`` mapping (used for text-ID rewriting).
    """
    if not gpas_work:
        return {}

    gpas_params = gpas_work[0].params

    # Serialise each element's value to a stable string key
    values_to_pseudonymize = []
    for item in gpas_work:
        val = item.element["value"]
        original_value = str(val) if not isinstance(val, dict) else json.dumps(val)
        values_to_pseudonymize.append(original_value)

    # Batch call — GpasUnavailableError always propagates (gPAS is down, no partial results).
    # Other exceptions respect processing_mode: 'skip' falls back to redaction, 'raise' propagates.
    try:
        batch_mapping = pseudonymizer.pseudonymize_batch(values_to_pseudonymize, gpas_params)
    except GpasUnavailableError:
        raise  # never swallow — caller must surface HTTP 503 and stop all processing
    except Exception as exc:
        if processing_mode == "skip":
            audit_log.warning(
                "gpas_batch_failed count=%d error_type=%s",
                len(values_to_pseudonymize), type(exc).__name__,
                exc_info=False,
            )
            for item in gpas_work:
                try:
                    perform_deidentification("redact", resource, item.element, {})
                except Exception as exc2:
                    audit_log.warning(
                        "fallback_redact_failed path=%s error_type=%s",
                        item.element.get("path", "?"), type(exc2).__name__,
                        exc_info=False,
                    )
            return {}
        raise

    # Write each pseudonym back into the resource
    for item in gpas_work:
        val = item.element["value"]
        original_value = str(val) if not isinstance(val, dict) else json.dumps(val)
        pseudonym = batch_mapping.get(original_value)

        if pseudonym is None:
            if processing_mode == "skip":
                audit_log.warning("gpas_no_pseudonym path=%s", item.element.get("path", "?"))
                try:
                    perform_deidentification("redact", resource, item.element, {})
                except Exception as exc2:
                    audit_log.warning(
                        "fallback_redact_failed path=%s error_type=%s",
                        item.element.get("path", "?"), type(exc2).__name__,
                        exc_info=False,
                    )
                continue
            raise ValueError(
                f"gPAS did not return a pseudonym for value (path={item.element['path']})"
            )

        path = item.element["path"].split(".")[1:]
        if len(path) == 0:
            resource.clear()
            continue
        ret = find_nodes(resource, path[:-1], [])
        _substitute_nodes(ret, path[-1], item.element["value"], pseudonym)

    return batch_mapping
