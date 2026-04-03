"""Application service layer for the MedAnon API."""

import json

GPAS_FATAL_JSON = json.dumps({
    "error": "gPAS service unavailable \u2014 stream halted to prevent partial results",
    "fatal": True,
})


def stream_trailer(resource_count: int) -> str:
    """Return the NDJSON stream completion trailer line.

    Clients detect truncated streams by the absence of this trailer.
    """
    return json.dumps({"__stream_complete": True, "resource_count": resource_count})
