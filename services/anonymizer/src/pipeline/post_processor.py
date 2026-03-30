"""Post-processing passes: FHIR reference rewriting and text-ID replacement.

All functions mutate the supplied object in place.
"""

from __future__ import annotations

import logging
import re

audit_log = logging.getLogger("medanon.audit")

_MAX_NESTING_DEPTH = 50


# ---------------------------------------------------------------------------
# Reference rewriting (after bundle processing)
# ---------------------------------------------------------------------------

def _rewrite_references(obj, ref_map: dict, _depth: int = 0) -> None:
    """Deep-walk *obj* and rewrite FHIR ``reference`` / ``url`` strings via *ref_map*."""
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, str) and value in ref_map and key in ("reference", "url"):
                audit_log.debug("reference_rewritten field=%s", key)
                obj[key] = ref_map[value]
            else:
                _rewrite_references(value, ref_map, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _rewrite_references(item, ref_map, _depth + 1)


# ---------------------------------------------------------------------------
# Text-ID rewriting (replace bare original IDs in free-text fields)
# ---------------------------------------------------------------------------

def _rewrite_text_ids(obj, id_map: dict, compiled=None, _depth: int = 0) -> None:
    """Walk all string values and replace any bare original ID with its pseudonym.

    Skips structural fields (``id``, ``reference``, ``url``, ``resourceType``)
    which are handled by other post-processors.
    """
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )
    if not id_map:
        return
    if compiled is None:
        parts = [re.escape(k) for k in id_map if k]
        if not parts:
            return
        compiled = re.compile(r"\b(" + "|".join(parts) + r")\b")

    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in ("id", "reference", "url", "resourceType"):
                continue
            value = obj[key]
            if isinstance(value, str):
                new_val = compiled.sub(lambda m: id_map[m.group(1)], value)
                if new_val != value:
                    audit_log.info("text_id_rewritten field=%s", key)
                    obj[key] = new_val
            else:
                _rewrite_text_ids(value, id_map, compiled, _depth + 1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_val = compiled.sub(lambda m: id_map[m.group(1)], item)
                if new_val != item:
                    obj[i] = new_val
            else:
                _rewrite_text_ids(item, id_map, compiled, _depth + 1)


# ---------------------------------------------------------------------------
# gPAS-based reference pseudonymization (cross-resource / NDJSON / bulk)
# ---------------------------------------------------------------------------

def _collect_reference_ids(obj, ids: set) -> None:
    """Collect all reference IDs from *obj* for batch pseudonymization."""
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str) and ref and "?" not in ref and not ref.startswith("#"):
            if ref.startswith("urn:uuid:"):
                resource_id = ref[len("urn:uuid:"):]
                if resource_id:
                    ids.add(resource_id)
            elif "/" in ref and not ref.startswith("http"):
                parts = ref.split("/")
                if len(parts) == 2 and parts[0] and parts[1]:
                    ids.add(parts[1])
        for value in obj.values():
            _collect_reference_ids(value, ids)
    elif isinstance(obj, list):
        for item in obj:
            _collect_reference_ids(item, ids)


def _pseudonymize_reference_string(ref: str, ref_mapping: dict) -> str:
    """Pseudonymize the ID portion of a FHIR reference string using *ref_mapping*."""
    if not ref or not isinstance(ref, str):
        return ref
    if "?" in ref or ref.startswith("#"):
        return ref

    if ref.startswith("urn:uuid:"):
        resource_id = ref[len("urn:uuid:"):]
        if resource_id and resource_id in ref_mapping:
            return f"urn:uuid:{ref_mapping[resource_id]}"
        return ref

    if "/" in ref and not ref.startswith("http"):
        parts = ref.split("/")
        if len(parts) == 2 and parts[0] and parts[1]:
            resource_type, resource_id = parts
            if resource_id in ref_mapping:
                return f"{resource_type}/{ref_mapping[resource_id]}"
        return ref

    return ref


def _apply_reference_pseudonyms(obj, ref_mapping: dict) -> None:
    """Apply pre-computed pseudonym mapping to all references in *obj*."""
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str):
            new_ref = _pseudonymize_reference_string(ref, ref_mapping)
            if new_ref != ref:
                audit_log.debug("reference_pseudonymized field=reference")
                obj["reference"] = new_ref
            if "display" in obj:
                audit_log.info("reference_display_redacted field=display")
                del obj["display"]
        for value in obj.values():
            _apply_reference_pseudonyms(value, ref_mapping)
    elif isinstance(obj, list):
        for item in obj:
            _apply_reference_pseudonyms(item, ref_mapping)


def _deep_rewrite_references_gpas(obj, gpas_params: dict, pseudonymizer) -> None:
    """Deep-walk *obj* and pseudonymize all direct reference IDs via gPAS.

    Three-pass approach:
    1. Collect all reference IDs.
    2. Batch-pseudonymize via *pseudonymizer*.
    3. Rewrite references using the mapping.
    """
    ref_ids: set[str] = set()
    _collect_reference_ids(obj, ref_ids)
    if not ref_ids:
        return
    ref_mapping = pseudonymizer.pseudonymize_batch(list(ref_ids), gpas_params)
    _apply_reference_pseudonyms(obj, ref_mapping)
