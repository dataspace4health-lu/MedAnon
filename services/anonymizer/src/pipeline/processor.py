"""FHIR de-identification / pseudonymization orchestrator.

Public surface:

- :func:`process_data` — single resource / Bundle / list (signature unchanged).
- :func:`process_data_batch` — batch of resources with cross-resource gPAS
  batching (one HTTP call for up to ``MEDANON_BATCH_SIZE`` resources).

All logic is delegated to the focused pipeline sub-modules:

- :mod:`pipeline.rule_matcher`    — FHIRPath evaluation + rule index
- :mod:`pipeline.action_dispatcher` — Pass 1 action dispatch + BatchWork accumulation
- :mod:`pipeline.gpas_orchestrator` — Pass 2 batch gPAS call
- :mod:`pipeline.post_processor`  — reference rewriting + text-ID replacement
- :mod:`pipeline.manifest`        — transformation manifest tagging
- :mod:`pipeline.ports`           — PseudonymizerPort Protocol

Dependency injection: pass a custom ``pseudonymizer`` (any object satisfying
:class:`~pipeline.ports.PseudonymizerPort`) to replace the default gPAS adapter.
All existing callers that omit the parameter continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import re

import fhirpathpy

from pipeline.manifest import _MANIFEST_ENABLED, _attach_manifest
from pipeline.rule_matcher import _get_rules_for_resource
from pipeline.action_dispatcher import dispatch_pass1
from pipeline.gpas_orchestrator import (
    run_gpas_batch,
    run_gpas_batch_for_batch,
    write_back_gpas_batch,
    _extract_gpas_params,
)
from pipeline.post_processor import (
    _deep_rewrite_references_gpas,
    _apply_reference_pseudonyms,
    _rewrite_references,
    _rewrite_text_ids,
    _collect_reference_ids,
)
from integrations.gpas.circuit_breaker import GpasUnavailableError

audit_log = logging.getLogger("medanon.audit")

# Register the FHIRPath log() invocation once at module level to avoid
# repeated global mutation on every rule evaluation (thread-safety fix).
fhirpathpy.engine.invocations["log"] = {
    "fn": lambda ctx, els: [{"path": x.path, "value": x.data} for x in els]
}

_BATCH_SIZE = int(os.environ.get("MEDANON_BATCH_SIZE", "200"))


def _get_default_pseudonymizer():
    from integrations.gpas.adapter import GpasPseudonymizerAdapter
    return GpasPseudonymizerAdapter()


def _processing_errors_mode(settings) -> str:
    return str(getattr(settings, "processing_errors", "raise")).lower()


# ---------------------------------------------------------------------------
# Per-resource finalize (Pass 2 + post-processing)
# ---------------------------------------------------------------------------

def _finalize_resource(
    resource: dict,
    settings,
    pseudonymizer,
    gpas_work: list,
    manifest_entries: list,
    processing_mode: str,
    precomputed_mapping: dict | None = None,
    precompiled_text_id_regex=None,
    precomputed_ref_mapping: dict | None = None,
) -> dict:
    """Run Pass 2 (gPAS) + post-processing for one resource.

    When *precomputed_mapping* is provided (N>1 batch path), applies the
    shared mapping directly with **zero** additional HTTP calls — used after
    :func:`run_gpas_batch_for_batch` has already fetched all pseudonyms.

    When *precomputed_ref_mapping* is provided, applies it directly instead
    of re-collecting reference IDs from the resource (avoids redundant deep walk).

    When *precomputed_mapping* is ``None`` (N=1 fast path), calls
    :func:`run_gpas_batch` which makes its own ``pseudonymize_batch`` call.
    """
    if precomputed_mapping is not None:
        batch_mapping = write_back_gpas_batch(
            resource, gpas_work, precomputed_mapping, processing_mode
        )
    else:
        batch_mapping = run_gpas_batch(resource, gpas_work, processing_mode, pseudonymizer)

    # Post-processing: pseudonymize FHIR references
    if getattr(settings, "rewrite_references", False):
        if precomputed_ref_mapping is not None:
            # N>1 path: mapping pre-computed by batch, skip redundant deep walk
            _apply_reference_pseudonyms(resource, precomputed_ref_mapping)
        else:
            gpas_params = _extract_gpas_params(settings)
            if gpas_params:
                _deep_rewrite_references_gpas(resource, gpas_params, pseudonymizer)

    # Post-processing: replace bare IDs embedded in free-text fields
    if getattr(settings, "rewrite_text_ids", False) and batch_mapping:
        id_text_map = {
            k: v
            for k, v in batch_mapping.items()
            if k and v and k != v and not k.startswith("{")
        }
        if id_text_map:
            audit_log.info("rewriting_text_ids count=%d", len(id_text_map))
            _rewrite_text_ids(resource, id_text_map, compiled=precompiled_text_id_regex)

    # Attach transformation manifest (when enabled)
    if _MANIFEST_ENABLED and manifest_entries:
        _attach_manifest(resource, manifest_entries)

    return resource


# ---------------------------------------------------------------------------
# Batch processing (unified)
# ---------------------------------------------------------------------------

def process_data_batch(
    resources: list[dict],
    settings,
    pseudonymizer=None,
) -> list[dict]:
    """De-identify / pseudonymize a batch of FHIR resources with cross-resource
    gPAS batching.

    For *N=1*, skips the cross-resource pre-fetch and goes straight to
    per-resource processing — identical performance to the old single-resource
    path.

    For *N>1*:
      1. Pass 1 on ALL resources (FHIRPath + non-gPAS actions, collect BatchWork)
      2. ONE gPAS HTTP call for all deduped values across all resources
      3. Pass 2 on each resource (cache hits, zero additional HTTP calls)
      4. Post-processing on each resource

    Args:
        resources:     List of FHIR resource dicts (not Bundles — those are
                       handled by ``_process_bundle``).
        settings:      Loaded :class:`~pipeline.config.Settings` instance.
        pseudonymizer: Optional :class:`~pipeline.ports.PseudonymizerPort`.

    Returns:
        List of processed resource dicts (same length and order as input).
    """
    if pseudonymizer is None:
        pseudonymizer = _get_default_pseudonymizer()

    if not resources:
        return []

    processing_mode = _processing_errors_mode(settings)

    # -- N=1 fast path: no cross-resource pre-fetch overhead ----------------
    if len(resources) == 1:
        resource = resources[0]
        try:
            rules = _get_rules_for_resource(resource, settings)
            manifest_entries: list[dict] = []
            gpas_work = dispatch_pass1(
                resource, rules, settings, manifest_entries, processing_mode
            )
            return [_finalize_resource(
                resource, settings, pseudonymizer,
                gpas_work, manifest_entries, processing_mode,
            )]
        except Exception as exc:
            rtype = resource.get("resourceType", "Unknown") if isinstance(resource, dict) else "Unknown"
            audit_log.error("batch_single_error resource_type=%s: %s", rtype, exc)
            if processing_mode != "skip":
                raise
            return [{"error": "processing error", "resourceType": rtype}]

    # -- N>1 batch path: cross-resource gPAS pre-fetch ----------------------
    gpas_params = _extract_gpas_params(settings)

    parsed: list[dict | None] = []
    all_gpas_works: list[list] = []
    all_manifest_entries: list[list] = []

    # Step 1: Pass 1 on all resources
    for resource in resources:
        try:
            rules = _get_rules_for_resource(resource, settings)
            manifest_entries_: list[dict] = []
            gpas_work = dispatch_pass1(
                resource, rules, settings, manifest_entries_, processing_mode
            )
        except Exception as exc:
            rtype = resource.get("resourceType", "Unknown") if isinstance(resource, dict) else "Unknown"
            audit_log.error("batch_pass1_error resource_type=%s: %s", rtype, exc)
            if processing_mode != "skip":
                raise
            parsed.append(None)
            all_gpas_works.append([])
            all_manifest_entries.append([])
            continue

        parsed.append(resource)
        all_gpas_works.append(gpas_work)
        all_manifest_entries.append(manifest_entries_)

    # Step 2: ONE gPAS HTTP call for all unique values across all resources
    shared_mapping = run_gpas_batch_for_batch(all_gpas_works, processing_mode, pseudonymizer, gpas_params)

    # Pre-compile the text-ID regex once for the whole batch so each resource
    # doesn't call re.compile() independently on the same pattern.
    _batch_text_id_regex = None
    if getattr(settings, "rewrite_text_ids", False) and shared_mapping:
        _batch_id_text_map = {
            k: v for k, v in shared_mapping.items()
            if k and v and k != v and not k.startswith("{")
        }
        if _batch_id_text_map:
            parts = [re.escape(k) for k in _batch_id_text_map if k]
            if parts:
                _batch_text_id_regex = re.compile(r"\b(" + "|".join(parts) + r")\b")

    # Pre-compute reference pseudonym mapping for the entire batch.
    # ONE collect pass + ONE gPAS call, then pass the mapping to each resource
    # so _finalize_resource skips the redundant per-resource deep walk.
    _batch_ref_mapping: dict | None = None
    if getattr(settings, "rewrite_references", False):
        _ref_gpas_params = _extract_gpas_params(settings)
        if _ref_gpas_params:
            _all_ref_ids: set[str] = set()
            for _r in parsed:
                if _r is not None:
                    _collect_reference_ids(_r, _all_ref_ids)
            if _all_ref_ids:
                try:
                    _batch_ref_mapping = pseudonymizer.pseudonymize_batch(
                        list(_all_ref_ids), _ref_gpas_params
                    )
                except GpasUnavailableError:
                    raise
                except Exception:
                    if processing_mode != "skip":
                        raise

    # Step 3+4: Per-resource finalize (write-back from shared mapping, zero HTTP)
    results: list[dict] = []
    for resource, gpas_work, manifest_entries_ in zip(
        parsed, all_gpas_works, all_manifest_entries
    ):
        if resource is None:
            results.append({"error": "pass1 error", "resourceType": "Unknown"})
            continue
        try:
            result = _finalize_resource(
                resource, settings, pseudonymizer,
                gpas_work, manifest_entries_, processing_mode,
                precomputed_mapping=shared_mapping,
                precompiled_text_id_regex=_batch_text_id_regex,
                precomputed_ref_mapping=_batch_ref_mapping,
            )
            results.append(result)
        except Exception as exc:
            rtype = resource.get("resourceType", "Unknown") if isinstance(resource, dict) else "Unknown"
            audit_log.error("batch_process_error resource_type=%s: %s", rtype, exc)
            if processing_mode != "skip":
                raise
            results.append({"error": "processing error", "resourceType": rtype})

    return results


# ---------------------------------------------------------------------------
# Bundle processing
# ---------------------------------------------------------------------------

def _process_bundle(resource: dict, settings, pseudonymizer) -> dict:
    entries = resource.get("entry", [])

    # Snapshot original resource IDs before any processing
    pre_ids: list[tuple] = []
    inner_resources: list[dict] = []
    entry_indices: list[int] = []
    for i, entry in enumerate(entries):
        r = entry.get("resource", {}) if isinstance(entry, dict) else {}
        if isinstance(r, dict) and "resourceType" in r:
            if "id" in r:
                pre_ids.append((r["resourceType"], r["id"]))
            else:
                pre_ids.append((r.get("resourceType"), None))
            inner_resources.append(r)
            entry_indices.append(i)
        else:
            pre_ids.append((None, None))

    # Batch-process all inner resources at once
    if inner_resources:
        processed = process_data_batch(inner_resources, settings, pseudonymizer)
        for idx, result in zip(entry_indices, processed):
            entries[idx]["resource"] = result

    # Build reference mapping from IDs that changed during processing
    ref_map: dict[str, str] = {}
    res_idx = 0
    for i, entry in enumerate(entries):
        old_type, old_id = pre_ids[i]
        if old_type is None:
            continue
        if i in entry_indices:
            r = entry.get("resource", {}) if isinstance(entry, dict) else {}
            new_id = r.get("id") if isinstance(r, dict) else None
            if old_id is not None and new_id is not None and old_id != new_id:
                ref_map[f"{old_type}/{old_id}"] = f"{old_type}/{new_id}"
            res_idx += 1

    if ref_map:
        audit_log.info("rewriting_references count=%d", len(ref_map))
        _rewrite_references(resource, ref_map)

    if getattr(settings, "rewrite_text_ids", False) and ref_map:
        id_text_map = {}
        for old_ref, new_ref in ref_map.items():
            old_id = old_ref.split("/", 1)[-1]
            new_id = new_ref.split("/", 1)[-1]
            if old_id and new_id and old_id != new_id:
                id_text_map[old_id] = new_id
        if id_text_map:
            audit_log.info("rewriting_text_ids count=%d", len(id_text_map))
            _rewrite_text_ids(resource, id_text_map)

    return resource


# ---------------------------------------------------------------------------
# Public entry point (backward-compatible)
# ---------------------------------------------------------------------------

def process_data(resource, settings, pseudonymizer=None):
    """De-identify / pseudonymize *resource* according to *settings*.

    Args:
        resource:      A FHIR resource dict, a Bundle dict, or a list of resources.
        settings:      Loaded :class:`~pipeline.config.Settings` instance.
        pseudonymizer: Optional :class:`~pipeline.ports.PseudonymizerPort` override.
                       Defaults to :class:`~integrations.gpas.adapter.GpasPseudonymizerAdapter`.

    Returns:
        The transformed resource (same type as input).
    """
    if pseudonymizer is None:
        pseudonymizer = _get_default_pseudonymizer()
    if isinstance(resource, list):
        return process_data_batch(resource, settings, pseudonymizer)
    if isinstance(resource, dict) and resource.get("resourceType") == "Bundle":
        return _process_bundle(resource, settings, pseudonymizer)
    return process_data_batch([resource], settings, pseudonymizer)[0]
