from utils.fhirpath import error, find_nodes

expected_params = ["substitute_with"]


def _substitute_nodes(node, key, value, new_value):
    if isinstance(node, list):
        for item in node:
            _substitute_nodes(item, key, value, new_value)
    elif isinstance(node, dict) and key in node:
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    node[key][idx] = new_value
        else:
            if node[key] == value:
                node[key] = new_value


def substitute_by_path(resource, el, params):
    if not all(param in params for param in expected_params):
        error(f"Missing params (expected {expected_params})")
    ret = resource
    path = el["path"]  # "Patient.name"
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        ret.clear()
        return
    ret = find_nodes(ret, path[:-1], [])
    _substitute_nodes(ret, path[-1], el["value"], params[expected_params[0]])
