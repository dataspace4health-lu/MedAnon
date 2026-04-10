"""Post-processing passes: FHIR reference rewriting and text-ID replacement.

All functions mutate the supplied object in place.
"""

from __future__ import annotations

import logging
import re

audit_log = logging.getLogger("medanon.audit")

_MAX_NESTING_DEPTH = 50

# ---------------------------------------------------------------------------
# Aho-Corasick text-ID replacement (optional, falls back to regex)
# ---------------------------------------------------------------------------

try:
    import ahocorasick as _aho

    _HAS_AHO = True
except ImportError:
    _HAS_AHO = False


def _build_text_id_automaton(id_map: dict):
    """Build an Aho-Corasick automaton for fast multi-pattern string matching.

    Returns the automaton, or *None* if pyahocorasick is not installed.
    """
    if not _HAS_AHO or not id_map:
        return None
    A = _aho.Automaton()
    for original, replacement in id_map.items():
        if original:
            A.add_word(original, (original, replacement))
    A.make_automaton()
    return A


def _aho_replace(text: str, automaton, id_map: dict) -> str:
    """Replace all matched IDs in *text* using the Aho-Corasick automaton.

    Only replaces on word boundaries to match the behavior of the regex
    ``\\b(id1|id2|...)\\b`` pattern.
    """
    # Collect matches (end_index, (original, replacement))
    matches = []
    for end_idx, (original, replacement) in automaton.iter(text):
        start_idx = end_idx - len(original) + 1
        # Word-boundary check
        if start_idx > 0 and text[start_idx - 1].isalnum():
            continue
        after = end_idx + 1
        if after < len(text) and text[after].isalnum():
            continue
        matches.append((start_idx, end_idx + 1, replacement))

    if not matches:
        return text

    # Build result from string slices (avoids per-character list allocation)
    parts = []
    prev = 0
    for start, end, replacement in matches:
        parts.append(text[prev:start])
        parts.append(replacement)
        prev = end
    parts.append(text[prev:])
    return "".join(parts)


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
            if (
                isinstance(value, str)
                and value in ref_map
                and key in ("reference", "url")
            ):
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


def _replace_text_ids(text: str, id_map: dict, automaton=None, compiled=None) -> str:
    """Replace all bare original IDs in *text* using Aho-Corasick or regex."""
    if automaton is not None:
        return _aho_replace(text, automaton, id_map)
    if compiled is not None:
        return compiled.sub(lambda m: id_map[m.group(1)], text)
    return text


def _build_text_id_matcher(id_map: dict) -> tuple:
    """Build the best available text-ID matcher for *id_map*.

    Returns ``(automaton, compiled_regex)``.  Exactly one will be non-None
    (Aho-Corasick preferred), or both None if *id_map* is empty.
    """
    if not id_map:
        return None, None
    automaton = _build_text_id_automaton(id_map)
    if automaton is not None:
        return automaton, None
    parts = [re.escape(k) for k in id_map if k]
    if not parts:
        return None, None
    return None, re.compile(r"\b(" + "|".join(parts) + r")\b")


_STRUCTURAL_FIELDS = frozenset(("id", "reference", "url", "resourceType"))


def _rewrite_text_ids(
    obj, id_map: dict, compiled=None, automaton=None, _depth: int = 0
) -> None:
    """Walk all string values and replace any bare original ID with its pseudonym.

    Skips structural fields (``id``, ``reference``, ``url``, ``resourceType``)
    which are handled by other post-processors.

    When *automaton* is provided (pyahocorasick installed), uses O(N+M)
    Aho-Corasick matching instead of the O(N*M) regex alternation.
    """
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )
    if not id_map:
        return
    # Build matcher on first call if neither was pre-built
    if automaton is None and compiled is None:
        automaton, compiled = _build_text_id_matcher(id_map)
        if automaton is None and compiled is None:
            return

    if isinstance(obj, dict):
        for key in obj.keys():
            if key in _STRUCTURAL_FIELDS:
                continue
            value = obj[key]
            if isinstance(value, str):
                new_val = _replace_text_ids(value, id_map, automaton, compiled)
                if new_val != value:
                    audit_log.debug("text_id_rewritten field=%s", key)
                    obj[key] = new_val
            else:
                _rewrite_text_ids(
                    value,
                    id_map,
                    automaton=automaton,
                    compiled=compiled,
                    _depth=_depth + 1,
                )
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                new_val = _replace_text_ids(item, id_map, automaton, compiled)
                if new_val != item:
                    obj[i] = new_val
            else:
                _rewrite_text_ids(
                    item,
                    id_map,
                    automaton=automaton,
                    compiled=compiled,
                    _depth=_depth + 1,
                )


# ---------------------------------------------------------------------------
# gPAS-based reference pseudonymization (cross-resource / NDJSON / bulk)
# ---------------------------------------------------------------------------


def _collect_reference_ids(obj, ids: set, _depth: int = 0) -> None:
    """Collect all reference IDs from *obj* for batch pseudonymization."""
    if _depth > _MAX_NESTING_DEPTH:
        return
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str) and ref and "?" not in ref and not ref.startswith("#"):
            if ref.startswith("urn:uuid:"):
                resource_id = ref[len("urn:uuid:") :]
                if resource_id:
                    ids.add(resource_id)
            elif "/" in ref and not ref.startswith("http"):
                parts = ref.split("/")
                if len(parts) == 2 and parts[0] and parts[1]:
                    ids.add(parts[1])
        for value in obj.values():
            _collect_reference_ids(value, ids, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _collect_reference_ids(item, ids, _depth + 1)


def _pseudonymize_reference_string(ref: str, ref_mapping: dict) -> str:
    """Pseudonymize the ID portion of a FHIR reference string using *ref_mapping*."""
    if not ref or not isinstance(ref, str):
        return ref
    if "?" in ref or ref.startswith("#"):
        return ref

    if ref.startswith("urn:uuid:"):
        resource_id = ref[len("urn:uuid:") :]
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


def _apply_reference_pseudonyms(obj, ref_mapping: dict, _depth: int = 0) -> None:
    """Apply pre-computed pseudonym mapping to all references in *obj*."""
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str):
            new_ref = _pseudonymize_reference_string(ref, ref_mapping)
            if new_ref != ref:
                audit_log.debug("reference_pseudonymized field=reference")
                obj["reference"] = new_ref
            if "display" in obj:
                audit_log.debug("reference_display_redacted field=display")
                del obj["display"]
        for value in obj.values():
            _apply_reference_pseudonyms(value, ref_mapping, _depth + 1)
    elif isinstance(obj, list):
        for item in obj:
            _apply_reference_pseudonyms(item, ref_mapping, _depth + 1)


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


# ---------------------------------------------------------------------------
# Merged post-processing walk (reference pseudonyms + text-ID replacement)
# ---------------------------------------------------------------------------


def _post_process_resource(
    obj,
    ref_mapping: dict | None,
    id_map: dict | None,
    automaton=None,
    compiled=None,
    _depth: int = 0,
) -> None:
    """Single-pass recursive walk that applies both reference pseudonymization
    and text-ID replacement in one traversal.

    Equivalent to calling ``_apply_reference_pseudonyms`` followed by
    ``_rewrite_text_ids`` but performs a single tree walk instead of two.

    Args:
        obj:          The FHIR resource (dict/list) to mutate in place.
        ref_mapping:  Pseudonym mapping for reference IDs (or None to skip).
        id_map:       ID→pseudonym map for text replacement (or None to skip).
        automaton:    Pre-built Aho-Corasick automaton (or None).
        compiled:     Pre-compiled regex (or None).
    """
    if _depth > _MAX_NESTING_DEPTH:
        raise ValueError(
            f"FHIR resource nesting exceeds maximum depth of {_MAX_NESTING_DEPTH}"
        )

    if isinstance(obj, dict):
        # --- Reference pseudonymization (same logic as _apply_reference_pseudonyms)
        if ref_mapping:
            ref = obj.get("reference")
            if isinstance(ref, str):
                new_ref = _pseudonymize_reference_string(ref, ref_mapping)
                if new_ref != ref:
                    audit_log.debug("reference_pseudonymized field=reference")
                    obj["reference"] = new_ref
                if "display" in obj:
                    audit_log.debug("reference_display_redacted field=display")
                    del obj["display"]

        # --- Text-ID replacement + recurse
        for key in list(obj.keys()):
            value = obj[key]
            if isinstance(value, str):
                # text-ID replacement (skip structural fields)
                if id_map and key not in _STRUCTURAL_FIELDS:
                    new_val = _replace_text_ids(value, id_map, automaton, compiled)
                    if new_val != value:
                        audit_log.debug("text_id_rewritten field=%s", key)
                        obj[key] = new_val
            else:
                _post_process_resource(
                    value,
                    ref_mapping,
                    id_map,
                    automaton,
                    compiled,
                    _depth + 1,
                )

    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            if isinstance(item, str):
                if id_map:
                    new_val = _replace_text_ids(item, id_map, automaton, compiled)
                    if new_val != item:
                        obj[i] = new_val
            else:
                _post_process_resource(
                    item,
                    ref_mapping,
                    id_map,
                    automaton,
                    compiled,
                    _depth + 1,
                )
