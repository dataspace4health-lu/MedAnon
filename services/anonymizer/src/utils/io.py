"""File I/O utilities for FHIR resources.

Separated from utils/fhirpath.py so that the FHIRPath module remains a
pure graph-traversal utility with no file system or process-exit side-effects.
"""

import json
import os
import sys

from rich import print


def read_resource_from_file(filename: str):
    """Read a FHIR resource from a JSON file and return the parsed dict."""
    try:
        with open(filename, "r") as jfile:
            json_data = json.load(jfile)
            print(f":thumbs_up: json {filename} read")
            return json_data
    except IOError as e:
        print(f":sad_but_relieved_face: File {filename} does not exist.")
        print(e)
        sys.exit(os.EX_OSFILE)
    except ValueError as e:
        print(":sad_but_relieved_face: Cannot parse json data.")
        print(e)
        sys.exit(os.EX_OSFILE)
