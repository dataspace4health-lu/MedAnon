"""utils.fhirpath — FHIRPath traversal helpers.

Lightweight, pure-Python FHIRPath evaluation used by the pipeline's
``rule_matcher`` and action implementations.  Only the subset of FHIRPath
needed for the rule engine is implemented — complex predicates and axis
navigation beyond simple dot-notation and ``where()`` are not supported.

Public API:
    find_nodes(resource, path)    — return list of (parent_dict, key) pairs
    error(msg, *args)             — raise FHIRPathError
    not_implemented(feature)      — raise NotImplementedError with context
"""

from datetime import datetime
import re

# Pre-compiled regex for stripping array-index notation (e.g. 'identifier[0]' → 'identifier').
# Used in find_nodes() which is called per-action per-matched-element.
_ARRAY_INDEX_RE = re.compile(r"\[\d+\]$")


def not_implemented(msg):
    raise NotImplementedError(msg)


def error(msg):
    raise ValueError(msg)


def find_nodes(node, path_list, wheres):
    if len(path_list) == 0:
        return node
    if isinstance(node, list):
        return [find_nodes(item, list(path_list), wheres) for item in node]
    if isinstance(node, dict):
        key = path_list[0]
        # Strip array-index notation from FHIRPath: 'identifier[0]' → 'identifier'
        # Fast check: skip regex when no '[' present (99%+ of keys)
        clean_key = key if "[" not in key else _ARRAY_INDEX_RE.sub("", key)
        if clean_key in node:
            return find_nodes(node[clean_key], path_list[1:], wheres)
        else:
            return []
    # node is a scalar (str, int, etc.) — no children to traverse
    return []


def get_date(date_str, date_format):
    try:
        return datetime.strptime(date_str, date_format)
    except ValueError:
        return None
