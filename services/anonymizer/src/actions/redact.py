from utils.fhirpath import find_nodes


def _del_nodes(node, key, value):
    if isinstance(node, list):
        for item in node:
            _del_nodes(item, key, value)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            # Rebuild the list excluding matching values to avoid skipping
            # elements when deleting by index during forward iteration.
            node[key] = [item for item in node[key] if item != value]
            if not node[key]:
                del node[key]
        else:
            del node[key]


def redact_by_path(resource, el, params):
    ret = resource
    path = el["path"]  # "Patient.name"
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        ret.clear()
        return
    ret = find_nodes(ret, path[:-1], [])
    for node in ret if isinstance(ret, list) else [ret]:
        _del_nodes(node, path[-1], el["value"])
