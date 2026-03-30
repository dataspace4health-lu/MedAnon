from datetime import datetime
import re

from utils.io import read_resource_from_file  # noqa: F401 — re-exported for backward compat


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
        clean_key = re.sub(r'\[\d+\]$', '', key)
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
