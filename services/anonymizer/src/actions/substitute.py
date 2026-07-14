"""substitute  value substitution / generalisation action.

Replaces matched FHIR field values with a static substitute or applies a
lookup from a configurable mapping table.  Used for generalisation (e.g.
mapping a specific diagnosis code to a broader category) and for replacing
identifying values with de-identified equivalents.
"""

from __future__ import annotations


import logging

from utils.fhirpath import _substitute_nodes, find_nodes

_log = logging.getLogger(__name__)
_DEFAULT_SUBSTITUTE = "[REDACTED]"

expected_params = ["substitute_with"]


def substitute_by_path(resource: dict, el: dict, params: dict) -> None:
    if "substitute_with" not in params:
        _log.warning(
            "substitute rule missing substitute_with param  defaulting to %r",
            _DEFAULT_SUBSTITUTE,
        )
    substitute_value = params.get("substitute_with", _DEFAULT_SUBSTITUTE)
    ret = resource
    path = el["path"]  # "Patient.name"
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in substitute  "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(ret, path[:-1], [])
    _substitute_nodes(ret, path[-1], el["value"], substitute_value)
