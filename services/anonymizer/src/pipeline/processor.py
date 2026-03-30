"""FHIR de-identification / pseudonymization orchestrator.

Public surface: :func:`process_data` — signature unchanged from previous monolith.
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

import fhirpathpy

from pipeline.manifest import _MANIFEST_ENABLED, _attach_manifest
from pipeline.rule_matcher import _get_rules_for_resource
from pipeline.action_dispatcher import dispatch_pass1
from pipeline.gpas_orchestrator import run_gpas_batch, _extract_gpas_params
from pipeline.post_processor import (
    _deep_rewrite_references_gpas,
    _rewrite_references,
    _rewrite_text_ids,
)

audit_log = logging.getLogger("medanon.audit")

# Register the FHIRPath log() invocation once at module level to avoid
# repeated global mutation on every rule evaluation (thread-safety fix).
fhirpathpy.engine.invocations["log"] = {
    "fn": lambda ctx, els: [{"path": x.path, "value": x.data} for x in els]
}


def _get_default_pseudonymizer():
    from integrations.gpas.adapter import GpasPseudonymizerAdapter
    return GpasPseudonymizerAdapter()


def _processing_errors_mode(settings) -> str:
    return str(getattr(settings, "processing_errors", "raise")).lower()


# ---------------------------------------------------------------------------
# Single-resource processing
# ---------------------------------------------------------------------------

def _process_single_resource(resource: dict, settings, pseudonymizer) -> dict:
    processing_mode = _processing_errors_mode(settings)
    applicable_rules = _get_rules_for_resource(resource, settings)
    manifest_entries: list[dict] = []

    # Pass 1 — evaluate FHIRPath, dispatch non-gPAS actions, collect deferred work
    gpas_work = dispatch_pass1(
        resource, applicable_rules, settings, manifest_entries, processing_mode
    )

    # Pass 2 — batch gPAS pseudonymization (no-op when gpas_work is empty)
    batch_mapping = run_gpas_batch(resource, gpas_work, processing_mode, pseudonymizer)

    # Post-processing: pseudonymize FHIR references across resources
    if getattr(settings, "rewrite_references", False):
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
            _rewrite_text_ids(resource, id_text_map)

    # Attach transformation manifest (when enabled)
    if _MANIFEST_ENABLED and manifest_entries:
        _attach_manifest(resource, manifest_entries)

    return resource


# ---------------------------------------------------------------------------
# Bundle processing
# ---------------------------------------------------------------------------

def _process_bundle(resource: dict, settings, pseudonymizer) -> dict:
    entries = resource.get("entry", [])

    # Snapshot original resource IDs before any processing
    pre_ids: list[tuple] = []
    for entry in entries:
        r = entry.get("resource", {}) if isinstance(entry, dict) else {}
        if isinstance(r, dict) and "resourceType" in r and "id" in r:
            pre_ids.append((r["resourceType"], r["id"]))
        else:
            pre_ids.append((None, None))

    # Process each entry
    for entry in entries:
        if isinstance(entry, dict) and "resource" in entry:
            entry["resource"] = process_data(entry["resource"], settings, pseudonymizer)

    # Build reference mapping from IDs that changed during processing
    ref_map: dict[str, str] = {}
    for i, entry in enumerate(entries):
        old_type, old_id = pre_ids[i]
        if old_type is None:
            continue
        r = entry.get("resource", {}) if isinstance(entry, dict) else {}
        new_id = r.get("id") if isinstance(r, dict) else None
        if new_id is not None and old_id != new_id:
            ref_map[f"{old_type}/{old_id}"] = f"{old_type}/{new_id}"

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
# Public entry point
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
        return [process_data(res, settings, pseudonymizer) for res in resource]
    if isinstance(resource, dict) and resource.get("resourceType") == "Bundle":
        return _process_bundle(resource, settings, pseudonymizer)
    return _process_single_resource(resource, settings, pseudonymizer)
