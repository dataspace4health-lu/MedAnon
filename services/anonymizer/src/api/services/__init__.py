"""Application service layer for the MedAnon API."""

from utils.json_fast import dumps as _json_dumps

GPAS_FATAL_JSON = _json_dumps(
    {
        "error": "gPAS service unavailable \u2014 stream halted to prevent partial results",
        "fatal": True,
    }
)


def stream_trailer(resource_count: int, score: dict | None = None) -> str:
    """Return the NDJSON stream completion trailer line.

    Clients detect truncated streams by the absence of this trailer.
    When ``score`` is provided it is embedded so frontends can display
    scoring results without a separate API call.
    """
    payload: dict = {"__stream_complete": True, "resource_count": resource_count}
    if score:
        payload["score"] = score
    return _json_dumps(payload)
