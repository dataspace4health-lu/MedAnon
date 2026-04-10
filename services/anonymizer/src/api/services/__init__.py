"""Application service layer for the MedAnon API."""

from utils.json_fast import dumps as _json_dumps

GPAS_FATAL_JSON = _json_dumps(
    {
        "error": "gPAS service unavailable \u2014 stream halted to prevent partial results",
        "fatal": True,
    }
)


def stream_trailer(resource_count: int) -> str:
    """Return the NDJSON stream completion trailer line.

    Clients detect truncated streams by the absence of this trailer.
    """
    return _json_dumps({"__stream_complete": True, "resource_count": resource_count})
