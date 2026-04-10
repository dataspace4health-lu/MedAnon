"""Pass 2: batch gPAS pseudonymization.

Collects the original values from all deferred BatchWork items, calls
the pseudonymizer in a single batch, then writes pseudonyms back into
the resource via the same node-substitution path used by the non-batch
actions.
"""

from __future__ import annotations

import logging
import threading

from utils.fhirpath import find_nodes
from actions.substitute import _substitute_nodes
from pipeline.action_dispatcher import BatchWork
from pipeline.rule_matcher import _resolve_rule_params
from pipeline.deidentify import perform_deidentification
from integrations.gpas.circuit_breaker import GpasUnavailableError

audit_log = logging.getLogger("medanon.audit")

# ---------------------------------------------------------------------------
# gPAS params cache — avoid re-scanning rules on every resource
# ---------------------------------------------------------------------------

_SENTINEL = object()
_gpas_params_lru: dict = {}
_gpas_params_lock = threading.Lock()


def _extract_gpas_params(settings) -> dict | None:
    """Return the params dict from the first ``gpas_pseudonymize`` rule, or *None*."""
    rules = getattr(settings, "rules", [])
    rules_key = getattr(settings, "filename", None)
    if rules_key is not None:
        cached = _gpas_params_lru.get(rules_key, _SENTINEL)
        if cached is not _SENTINEL:
            return cached
    result = None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("action") == "gpas_pseudonymize":
            result = _resolve_rule_params(rule, settings)
            break
    if rules_key is not None:
        with _gpas_params_lock:
            _gpas_params_lru[rules_key] = result
            if len(_gpas_params_lru) > 64:
                oldest = next(iter(_gpas_params_lru))
                _gpas_params_lru.pop(oldest, None)
    return result


# ---------------------------------------------------------------------------
#  batch runner
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
        values_to_pseudonymize.append(item.serialized_value)

    # Batch call — GpasUnavailableError always propagates (gPAS is down, no partial results).
    # Other exceptions respect processing_mode: 'skip' falls back to redaction, 'raise' propagates.
    try:
        batch_mapping = pseudonymizer.pseudonymize_batch(
            values_to_pseudonymize, gpas_params
        )
    except GpasUnavailableError:
        raise  # never swallow — caller must surface HTTP 503 and stop all processing
    except Exception as exc:
        if processing_mode == "skip":
            audit_log.warning(
                "gpas_batch_failed count=%d error_type=%s",
                len(values_to_pseudonymize),
                type(exc).__name__,
                exc_info=False,
            )
            for item in gpas_work:
                try:
                    perform_deidentification("redact", resource, item.element, {})
                except Exception as exc2:
                    audit_log.warning(
                        "fallback_redact_failed path=%s error_type=%s",
                        item.element.get("path", "?"),
                        type(exc2).__name__,
                        exc_info=False,
                    )
            return {}
        raise

    # Write each pseudonym back into the resource
    for item in gpas_work:
        original_value = item.serialized_value
        pseudonym = batch_mapping.get(original_value)

        if pseudonym is None:
            if processing_mode == "skip":
                audit_log.warning(
                    "gpas_no_pseudonym path=%s", item.element.get("path", "?")
                )
                try:
                    perform_deidentification("redact", resource, item.element, {})
                except Exception as exc2:
                    audit_log.warning(
                        "fallback_redact_failed path=%s error_type=%s",
                        item.element.get("path", "?"),
                        type(exc2).__name__,
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


def write_back_gpas_batch(
    resource: dict,
    gpas_work: list[BatchWork],
    batch_mapping: dict,
    processing_mode: str,
) -> dict:
    """Apply a pre-computed gPAS mapping to *resource* without making HTTP calls.

    Used in the N>1 batch path where :func:`run_gpas_batch_for_batch` has
    already fetched the shared mapping.  This function does only the
    write-back step — no pseudonymizer call is made.

    Returns the same *batch_mapping* passed in (for text-ID rewriting parity
    with :func:`run_gpas_batch`).
    """
    if not gpas_work or not batch_mapping:
        return batch_mapping

    for item in gpas_work:
        original_value = item.serialized_value
        pseudonym = batch_mapping.get(original_value)

        if pseudonym is None:
            if processing_mode == "skip":
                audit_log.warning(
                    "gpas_no_pseudonym path=%s", item.element.get("path", "?")
                )
                try:
                    perform_deidentification("redact", resource, item.element, {})
                except Exception as exc2:
                    audit_log.warning(
                        "fallback_redact_failed path=%s error_type=%s",
                        item.element.get("path", "?"),
                        type(exc2).__name__,
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


def run_gpas_batch_for_batch(
    gpas_works: list[list[BatchWork]],
    processing_mode: str,
    pseudonymizer,
    gpas_params: dict | None,
) -> dict:
    """Pre-fetch pseudonyms for all resources in a staged batch with ONE gPAS call.

    After this function returns, the pseudonymizer's internal cache is warm.
    Subsequent ``run_gpas_batch`` calls for each individual resource will find
    their values already cached — zero additional gPAS HTTP round-trips.

    Args:
        gpas_works:      One list of :class:`BatchWork` items per resource.
        processing_mode: ``'raise'`` or ``'skip'``.
        pseudonymizer:   :class:`~pipeline.ports.PseudonymizerPort` implementation.
        gpas_params:     gPAS call parameters (domain, operation, etc.).

    Returns:
        The ``{original: pseudonym}`` mapping shared across all resources.
        Empty dict when no work items exist or gPAS is unavailable in skip mode.
    """
    if not gpas_params:
        # No gPAS rules configured — nothing to pre-fetch.
        return {}

    all_values: list[str] = []
    for work_list in gpas_works:
        for item in work_list:
            all_values.append(item.serialized_value)

    if not all_values:
        return {}

    # Deduplicate while preserving order — avoids sending the same value twice.
    unique_values = list(dict.fromkeys(all_values))

    try:
        batch_mapping = pseudonymizer.pseudonymize_batch(unique_values, gpas_params)
    except GpasUnavailableError:
        raise  # always propagate — never emit partial results
    except Exception as exc:
        if processing_mode == "skip":
            audit_log.warning(
                "gpas_batch_for_batch_failed unique_values=%d error_type=%s",
                len(unique_values),
                type(exc).__name__,
                exc_info=False,
            )
            return {}
        raise

    audit_log.debug(
        "gpas_prefetch unique_values=%d batch_size=%d",
        len(unique_values),
        len(gpas_works),
    )
    return batch_mapping
