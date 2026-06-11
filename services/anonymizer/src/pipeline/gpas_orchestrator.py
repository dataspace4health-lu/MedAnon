"""Pass 2: batch gPAS pseudonymization.

Collects the original values from all deferred PseudonymizationTask items, calls
the pseudonymizer in a single batch, then writes pseudonyms back into
the resource via the same node-substitution path used by the non-batch
actions.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import as_completed

from utils.fhirpath import find_nodes
from utils.thread_pool import get_executor
from actions.substitute import _substitute_nodes
from pipeline.action_dispatcher import PseudonymizationTask
from pipeline.manifest import _MANIFEST_ENABLED
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


def clear_gpas_params_cache() -> None:
    """Clear the per-profile gPAS params cache (called on config change)."""
    with _gpas_params_lock:
        _gpas_params_lru.clear()


def _extract_gpas_params(settings) -> dict | None:
    """Return the params dict from the first ``gpas_pseudonymize`` rule, or *None*.

    Cached per ``(filename, config_hash)`` — content-keyed so an in-place
    profile edit can never serve stale gPAS params.
    """
    rules = getattr(settings, "rules", [])
    from pipeline.rule_matcher import _settings_cache_key

    rules_key = _settings_cache_key(settings)
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


def pseudonymize_resource_identifiers(
    resource: dict,
    gpas_work: list[PseudonymizationTask],
    processing_mode: str,
    pseudonymizer,
    manifest_entries: list | None = None,
) -> dict:
    """Batch-pseudonymize all deferred gPAS work items.

    Calls ``pseudonymizer.pseudonymize_batch()`` once for all values,
    then writes each pseudonym back into *resource* in place.

    Args:
        resource:         The resource being processed (mutated in place).
        gpas_work:        Deferred work items from Pass 1.
        processing_mode:  ``'raise'`` or ``'skip'`` on error.
        pseudonymizer:    :class:`~pipeline.ports.PseudonymizerPort` implementation.
        manifest_entries: List to append manifest entries to (actual outcome action).

    Returns:
        The ``{original: pseudonym}`` mapping (used for text-ID rewriting).
    """
    if not gpas_work:
        return {}

    # Group work items by their resolved gpas_domain so each distinct domain
    # gets exactly one HTTP call.  A single resource may have fields that map
    # to different leaf domains (e.g. Patient.id → patient-admin, but a
    # contained Observation.id → observation).
    domain_to_params: dict[str, dict] = {}
    domain_to_values: dict[str, list[str]] = {}
    for item in gpas_work:
        domain = item.params.get("gpas_domain", "").strip()
        if not domain:
            # An empty domain would silently route to gPAS's default domain,
            # producing pseudonyms that are inconsistent with related resources.
            # Fail loudly so misconfigured rules are caught before any HTTP call.
            raise ValueError(
                f"gpas_domain is required but missing or empty for rule "
                f"targeting path '{item.element.get('path', '?')}'. "
                "Check your config profile's gpas_domain parameter."
            )
        if domain not in domain_to_params:
            domain_to_params[domain] = item.params
            domain_to_values[domain] = []
        domain_to_values[domain].append(item.serialized_value)

    batch_mapping: dict = {}

    def _call_domain(domain: str) -> tuple[str, dict | None, Exception | None]:
        unique_values = list(dict.fromkeys(domain_to_values[domain]))
        try:
            partial = pseudonymizer.pseudonymize_batch(
                unique_values, domain_to_params[domain]
            )
            return domain, partial, None
        except GpasUnavailableError as exc:
            return domain, None, exc
        except Exception as exc:
            return domain, None, exc

    domains = list(domain_to_values.keys())
    if len(domains) <= 1:
        # Single domain — skip thread pool overhead
        for domain in domains:
            _, partial, exc = _call_domain(domain)
            if exc is not None:
                if isinstance(exc, GpasUnavailableError):
                    raise exc
                if processing_mode == "skip":
                    audit_log.warning(
                        "gpas_batch_failed domain=%s count=%d error_type=%s",
                        domain,
                        len(domain_to_values[domain]),
                        type(exc).__name__,
                        exc_info=False,
                    )
                    # Items for this domain will get fallback-redacted in
                    # the write-back loop below (batch_mapping miss).
                    continue
                raise exc
            if partial:
                batch_mapping.update(partial)
    else:
        # Multiple domains — call in parallel
        pool = get_executor()
        futures = {}
        for d in domains:
            try:
                futures[pool.submit(_call_domain, d)] = d
            except TimeoutError:
                audit_log.error(
                    "gpas_submit_timeout domain=%s — thread pool saturated", d
                )
                if processing_mode != "skip":
                    raise
                # Items for this domain will get fallback-redacted in the
                # write-back loop below (batch_mapping miss).
        for fut in as_completed(futures):
            domain, partial, exc = fut.result()
            if exc is not None:
                if isinstance(exc, GpasUnavailableError):
                    raise exc
                if processing_mode != "skip":
                    raise exc
                audit_log.warning(
                    "gpas_batch_failed domain=%s count=%d error_type=%s",
                    domain,
                    len(domain_to_values[domain]),
                    type(exc).__name__,
                    exc_info=False,
                )
                # Items for this domain will get fallback-redacted in the
                # write-back loop below (batch_mapping miss).
                continue
            if partial:
                batch_mapping.update(partial)

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
                    if _MANIFEST_ENABLED and manifest_entries is not None:
                        manifest_entries.append(
                            {
                                "rule": item.rule.get(
                                    "name", item.rule.get("match", "?")
                                ),
                                "action": "redact",
                                "path": item.element.get("path", "?"),
                            }
                        )
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
            audit_log.warning(
                "gpas_root_path_skipped path=%s — refusing to clear entire resource",
                item.element.get("path", "?"),
            )
            if processing_mode != "skip":
                raise ValueError(
                    f"Empty path after removing resource type root in pseudonymize_resource_identifiers "
                    f"— refusing to clear entire resource (original path: {item.element['path']!r})"
                )
            continue
        ret = find_nodes(resource, path[:-1], [])
        # Restore urn:uuid: prefix when the original field value had it. The prefix
        # was stripped in action_dispatcher to ensure the bare UUID reaches gPAS (so
        # "urn:uuid:abc-123" and "abc-123" get the same pseudonym). We put it back
        # so the rewritten reference remains a valid urn:uuid: URI.
        _orig_val = item.element["value"]
        _write_back = (
            f"urn:uuid:{pseudonym}"
            if isinstance(_orig_val, str) and _orig_val.startswith("urn:uuid:")
            else pseudonym
        )
        _substitute_nodes(ret, path[-1], _orig_val, _write_back)
        if _MANIFEST_ENABLED and manifest_entries is not None:
            manifest_entries.append(
                {
                    "rule": item.rule.get("name", item.rule.get("match", "?")),
                    "action": item.rule.get("action", "gpas_pseudonymize"),
                    "path": item.element.get("path", "?"),
                }
            )

    return batch_mapping


def depseudonymize_resource_identifiers(
    resource: dict,
    depseudo_work: list[PseudonymizationTask],
    processing_mode: str,
) -> None:
    """Batch-de-pseudonymize all deferred gPAS depseudo work items.

    Calls :func:`gpas_depseudonymize_batch` once for all values grouped by
    domain, then writes each original back into *resource* in place.

    Args:
        resource:        The resource being processed (mutated in place).
        depseudo_work:   Deferred depseudo work items from Pass 1.
        processing_mode: ``'raise'`` or ``'skip'`` on error.
    """
    if not depseudo_work:
        return

    from integrations.gpas.client import gpas_depseudonymize_batch

    # Group by domain
    domain_to_params: dict[str, dict] = {}
    domain_to_values: dict[str, list[str]] = {}
    for item in depseudo_work:
        domain = item.params.get("gpas_domain", "")
        if domain not in domain_to_params:
            domain_to_params[domain] = item.params
            domain_to_values[domain] = []
        domain_to_values[domain].append(item.serialized_value)

    batch_mapping: dict = {}
    for domain, values in domain_to_values.items():
        unique_values = list(dict.fromkeys(values))
        try:
            partial = gpas_depseudonymize_batch(unique_values, domain_to_params[domain])
            batch_mapping.update(partial)
        except GpasUnavailableError:
            raise
        except Exception as exc:
            if processing_mode == "skip":
                audit_log.warning(
                    "gpas_depseudo_batch_failed domain=%s count=%d error_type=%s",
                    domain,
                    len(unique_values),
                    type(exc).__name__,
                    exc_info=False,
                )
                continue
            raise

    # Write back originals
    for item in depseudo_work:
        original = batch_mapping.get(item.serialized_value)
        if original is None:
            if processing_mode == "skip":
                audit_log.warning(
                    "gpas_depseudo_no_original path=%s", item.element.get("path", "?")
                )
                continue
            raise ValueError(
                f"gPAS did not return an original for pseudonym (path={item.element['path']})"
            )

        path = item.element["path"].split(".")[1:]
        if len(path) == 0:
            audit_log.warning(
                "gpas_depseudo_root_path_skipped path=%s",
                item.element.get("path", "?"),
            )
            if processing_mode != "skip":
                raise ValueError(
                    f"Empty path after removing resource type root in depseudonymize "
                    f"— refusing to clear entire resource (original path: {item.element['path']!r})"
                )
            continue
        ret = find_nodes(resource, path[:-1], [])
        _substitute_nodes(ret, path[-1], item.element["value"], original)


def apply_pseudonym_mapping(
    resource: dict,
    gpas_work: list[PseudonymizationTask],
    batch_mapping: dict,
    processing_mode: str,
    manifest_entries: list | None = None,
) -> dict:
    """Apply a pre-computed gPAS mapping to *resource* without making HTTP calls.

    Used in the N>1 batch path where :func:`pseudonymize_identifier_batch` has
    already fetched the shared mapping.  This function does only the
    write-back step — no pseudonymizer call is made.

    Returns the same *batch_mapping* passed in (for text-ID rewriting parity
    with :func:`pseudonymize_resource_identifiers`).
    """
    if not gpas_work:
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
                    if _MANIFEST_ENABLED and manifest_entries is not None:
                        manifest_entries.append(
                            {
                                "rule": item.rule.get(
                                    "name", item.rule.get("match", "?")
                                ),
                                "action": "redact",
                                "path": item.element.get("path", "?"),
                            }
                        )
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
            audit_log.warning(
                "gpas_writeback_root_path_skipped path=%s",
                item.element.get("path", "?"),
            )
            if processing_mode != "skip":
                raise ValueError(
                    f"Empty path after removing resource type root in apply_pseudonym_mapping "
                    f"— refusing to clear entire resource (original path: {item.element['path']!r})"
                )
            continue
        ret = find_nodes(resource, path[:-1], [])
        _orig_val = item.element["value"]
        _write_back = (
            f"urn:uuid:{pseudonym}"
            if isinstance(_orig_val, str) and _orig_val.startswith("urn:uuid:")
            else pseudonym
        )
        _substitute_nodes(ret, path[-1], _orig_val, _write_back)
        if _MANIFEST_ENABLED and manifest_entries is not None:
            manifest_entries.append(
                {
                    "rule": item.rule.get("name", item.rule.get("match", "?")),
                    "action": item.rule.get("action", "gpas_pseudonymize"),
                    "path": item.element.get("path", "?"),
                }
            )

    return batch_mapping


def pseudonymize_identifier_batch(
    gpas_works: list[list[PseudonymizationTask]],
    processing_mode: str,
    pseudonymizer,
    gpas_params: dict | None,
    extra_values: list[str] | None = None,
    extra_values_by_domain: "dict[str, list[str]] | None" = None,
    exclude_cached: set[str] | None = None,
) -> dict:
    """Pre-fetch pseudonyms for all resources in a staged batch with ONE gPAS call.

    After this function returns, the pseudonymizer's internal cache is warm.
    Subsequent ``pseudonymize_resource_identifiers`` calls for each individual resource will find
    their values already cached — zero additional gPAS HTTP round-trips.

    Args:
        gpas_works:      One list of :class:`PseudonymizationTask` items per resource.
        processing_mode: ``'raise'`` or ``'skip'``.
        pseudonymizer:   :class:`~pipeline.ports.PseudonymizerPort` implementation.
        gpas_params:     gPAS call parameters (domain, operation, etc.).
        extra_values:    Additional values to include in the batch (e.g. reference IDs),
                         all routed to the default ``gpas_params`` domain.  Mutually
                         exclusive with *extra_values_by_domain* — if both are provided
                         *extra_values_by_domain* takes precedence.
        extra_values_by_domain:  Per-domain reference ID buckets: ``{domain: [id, ...]}``.
                         When provided, each bucket is sent to its own gPAS domain so
                         that e.g. Patient references use ``spe.direct.patient-admin``
                         instead of the default domain.
        exclude_cached:  Values already pseudonymized in prior streaming chunks.
                         These are skipped before the HTTP call (they are in the
                         pseudonymizer's cache anyway, but skipping avoids even
                         the cache lookup overhead on the gPAS side).

    Returns:
        The ``{original: pseudonym}`` mapping shared across all resources.
        Empty dict when no work items exist or gPAS is unavailable in skip mode.
    """
    if not gpas_params:
        # No gPAS rules configured — nothing to pre-fetch.
        return {}

    # Group values by their resolved gpas_domain so each distinct domain gets
    # exactly one HTTP call.  Without this, domain_map routing would send all
    # resource types to the same domain during the N>1 batch pre-fetch.
    domain_to_params: dict[str, dict] = {}
    domain_to_values: dict[str, list[str]] = {}

    for work_list in gpas_works:
        for item in work_list:
            domain = (
                item.params.get("gpas_domain") or gpas_params.get("gpas_domain", "")
            ).strip()
            if not domain:
                raise ValueError(
                    f"gpas_domain is required but missing or empty for rule "
                    f"targeting path '{item.element.get('path', '?')}'. "
                    "Check your config profile's gpas_domain parameter."
                )
            if domain not in domain_to_params:
                domain_to_params[domain] = item.params
                domain_to_values[domain] = []
            domain_to_values[domain].append(item.serialized_value)

    # Reference IDs are stored under sentinel keys (prefixed "\x00extra") so they
    # are processed LAST — PseudonymizationTask results (per-type domain_map routing) take
    # precedence over the default domain when both cover the same original value.
    # extra_values_by_domain uses per-domain sentinels "\x00extra\x00{domain}" for
    # typed routing; extra_values (legacy) uses the single "\x00extra" sentinel.
    _EXTRA_KEY = "\x00extra"
    if extra_values_by_domain:
        for _ref_domain, _ref_vals in extra_values_by_domain.items():
            _sentinel = f"{_EXTRA_KEY}\x00{_ref_domain}"
            _ref_params = dict(gpas_params)
            _ref_params["gpas_domain"] = _ref_domain
            domain_to_params[_sentinel] = _ref_params
            domain_to_values[_sentinel] = list(_ref_vals)
    elif extra_values:
        domain_to_params[_EXTRA_KEY] = gpas_params
        domain_to_values[_EXTRA_KEY] = list(extra_values)

    if not domain_to_values:
        return {}

    combined_mapping: dict = {}

    # Separate primary domains (data IDs) from extra domains (reference IDs).
    # Primaries can run in parallel; extras must run after because they filter
    # values against combined_mapping (domain_map routing precedence).
    primary_domains = [d for d in domain_to_values if not d.startswith(_EXTRA_KEY)]
    extra_domains = [d for d in domain_to_values if d.startswith(_EXTRA_KEY)]

    def _call_domain(
        domain: str, values: list[str]
    ) -> tuple[str, dict | None, Exception | None]:
        """Call gPAS for a single domain. Returns (domain, mapping, error)."""
        try:
            partial = pseudonymizer.pseudonymize_batch(values, domain_to_params[domain])
            return domain, partial, None
        except GpasUnavailableError as exc:
            return domain, None, exc
        except Exception as exc:
            return domain, None, exc

    def _process_result(
        domain: str, partial: dict | None, exc: Exception | None
    ) -> None:
        """Handle result from a single domain call."""
        if exc is not None:
            if isinstance(exc, GpasUnavailableError):
                raise exc
            if processing_mode == "skip":
                audit_log.warning(
                    "gpas_batch_for_batch_failed domain=%s unique_values=%d error_type=%s",
                    domain,
                    len(domain_to_values.get(domain, [])),
                    type(exc).__name__,
                    exc_info=False,
                )
                return
            raise exc
        if partial:
            combined_mapping.update(partial)

    def _prepare_values(domain: str) -> list[str]:
        """Dedup + exclude cached for a primary domain."""
        unique_values = list(dict.fromkeys(domain_to_values[domain]))
        if exclude_cached:
            unique_values = [v for v in unique_values if v not in exclude_cached]
        return unique_values

    # --- Primary domains: parallel when >1 ---
    if len(primary_domains) <= 1:
        for domain in primary_domains:
            unique_values = _prepare_values(domain)
            if not unique_values:
                continue
            d, partial, exc = _call_domain(domain, unique_values)
            _process_result(d, partial, exc)
    else:
        pool = get_executor()
        futures = {}
        for domain in primary_domains:
            unique_values = _prepare_values(domain)
            if not unique_values:
                continue
            futures[pool.submit(_call_domain, domain, unique_values)] = domain
        for fut in as_completed(futures):
            d, partial, exc = fut.result()
            _process_result(d, partial, exc)

    # --- Extra domains: sequential (filter by combined_mapping) ---
    for domain in extra_domains:
        values = [v for v in domain_to_values[domain] if v not in combined_mapping]
        unique_values = list(dict.fromkeys(values))
        if not unique_values:
            continue
        d, partial, exc = _call_domain(domain, unique_values)
        _process_result(d, partial, exc)

    # --- Supplement excluded values not yet in combined_mapping ---
    # Values in exclude_cached were skipped from the gPAS HTTP call assuming they
    # remain in L1 cache.  With a large L1 (default 300K) most will be hot;
    # with Redis L2 configured, evicted values are warm (~1 ms).
    # Strategy: try cache-only first (zero network I/O in hot/warm state), then
    # fall back to pseudonymize_batch only for the cold remainder.
    if exclude_cached:
        for domain in primary_domains:
            all_vals = list(dict.fromkeys(domain_to_values[domain]))
            need = [
                v for v in all_vals if v in exclude_cached and v not in combined_mapping
            ]
            if not need:
                continue
            # Pass 1: pure cache lookup — hot L1 hit → O(1) shard lock, no HTTP;
            # warm L2 hit → one Redis mget (~1 ms), promotes to L1.
            try:
                cached_partial = pseudonymizer.lookup_cache_batch(
                    need, domain_to_params[domain]
                )
                combined_mapping.update(cached_partial)
            except Exception:
                cached_partial = {}
            # Pass 2: anything still missing must go to gPAS (cold miss).
            cold = [v for v in need if v not in combined_mapping]
            if not cold:
                continue
            try:
                partial = pseudonymizer.pseudonymize_batch(
                    cold, domain_to_params[domain]
                )
                combined_mapping.update(partial)
            except Exception as exc:
                audit_log.warning(
                    "gpas_excluded_supplement_failed domain=%s count=%d error_type=%s",
                    domain,
                    len(cold),
                    type(exc).__name__,
                    exc_info=False,
                )

    audit_log.debug(
        "gpas_prefetch domains=%d batch_size=%d",
        len(domain_to_values),
        len(gpas_works),
    )
    return combined_mapping


# Backward-compatible aliases.
run_gpas_batch = pseudonymize_resource_identifiers
run_gpas_depseudo_batch = depseudonymize_resource_identifiers
write_back_gpas_batch = apply_pseudonym_mapping
run_gpas_batch_for_batch = pseudonymize_identifier_batch
