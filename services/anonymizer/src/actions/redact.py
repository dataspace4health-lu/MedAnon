from __future__ import annotations

from typing import Any

from utils.fhirpath import find_nodes


def _del_nodes(node: Any, key: str, value: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _del_nodes(item, key, value)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list) and value is not None:
            # Rebuild the list excluding matching values to avoid skipping
            # elements when deleting by index during forward iteration.
            node[key] = [item for item in node[key] if item != value]
            if not node[key]:
                del node[key]
        else:
            # value is None means the field content is unknown (FHIRPath eval
            # failure fallback path) — remove the entire field unconditionally
            # so no PHI survives in list fields where value-equality matching
            # would otherwise be a no-op.
            del node[key]


def redact_by_path(resource: dict, el: dict, params: dict) -> None:
    ret = resource
    path = el["path"]  # "Patient.name"
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in redact — "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(ret, path[:-1], [])
    for node in ret if isinstance(ret, list) else [ret]:
        _del_nodes(node, path[-1], el["value"])
