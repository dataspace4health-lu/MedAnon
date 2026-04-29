from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Union

from utils.crypto import bounded_random
from utils.fhirpath import error, find_nodes, get_date

expected_params = ["min", "max"]
date_format = "%Y-%m-%d"


def _perturb(real_value: Union[int, float, date], noise_range: list, is_date: bool = False) -> Any:
    noise = bounded_random(noise_range[0], noise_range[1])
    if not is_date:
        return real_value + noise
    return (real_value + timedelta(days=noise)).strftime(date_format)


def _perturb_nodes(node: Any, key: str, value: Any, noise_range: list) -> None:
    if isinstance(node, list):
        for item in node:
            _perturb_nodes(item, key, value, noise_range)
    elif isinstance(node, dict) and (key in node):
        if isinstance(node[key], list):
            for idx, data in enumerate(node[key]):
                if data == value:
                    elem = node[key][idx]
                    if isinstance(elem, (int, float)) and not isinstance(elem, bool):
                        node[key][idx] = _perturb(elem, noise_range)
                    elif get_date(elem, date_format):
                        node[key][idx] = _perturb(
                            get_date(elem, date_format), noise_range, True
                        )
                    else:
                        error(f"{type(node[key][idx])} is not a number")
        else:
            elem = node[key]
            if isinstance(elem, (int, float)) and not isinstance(elem, bool):
                node[key] = _perturb(elem, noise_range)
            elif get_date(elem, date_format):
                node[key] = _perturb(get_date(elem, date_format), noise_range, True)
            else:
                error(f"{type(node[key])} is not a number")


def perturb_by_path(resource: dict, el: dict, params: dict) -> None:  # ONLY FOR NUMBERS AND DATES
    if not all(param in params for param in expected_params):
        error(f"Missing params (expected {expected_params})")
    ret = resource
    path = el["path"]
    path = path.split(".")[1:]  # Remove root
    if len(path) == 0:
        raise ValueError(
            f"Empty path after removing resource type root in perturb — "
            f"refusing to clear entire resource (original path: {el['path']!r})"
        )
    ret = find_nodes(ret, path[:-1], [])
    _perturb_nodes(
        ret, path[-1], el["value"], [params[elem] for elem in expected_params]
    )
