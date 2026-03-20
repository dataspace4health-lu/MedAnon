from datetime import datetime
import json
import os
import re
import sys
from rich import print


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


def read_resource_from_file(filename: str):
    """
    Read a fhir resource from file and return the json data
    """
    try:
        with open(filename, 'r') as jfile:
            json_data = json.load(jfile)
            print(f":thumbs_up: json {filename} read")
            return json_data
    except IOError as e:
        print(
            f":sad_but_relieved_face: File {filename} does not exist.")
        print(e)
        sys.exit(os.EX_OSFILE)
    except ValueError as e:
        print(
            ":sad_but_relieved_face: Cannot parse json data.")
        print(e)
        sys.exit(os.EX_OSFILE)
