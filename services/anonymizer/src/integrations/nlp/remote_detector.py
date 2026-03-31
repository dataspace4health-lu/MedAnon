"""Remote NLP detector — calls the NLP microservice over HTTP.

Drop-in replacement for the local ``_analyze_and_replace`` calls when
``NLP_SERVICE_URL`` is set. The calling interface in ``nlp_detect_by_path``
remains unchanged; the choice of local vs. remote is made at call time.
"""

from __future__ import annotations

import json
import logging
import os
from urllib import error as _uerr
from urllib import request as _ureq

_log = logging.getLogger("medanon.nlp.remote")


def _nlp_service_url(path: str) -> str:
    base = os.environ.get("NLP_SERVICE_URL", "").rstrip("/")
    return f"{base}{path}"


def analyze_and_replace_remote(
    text: str,
    entities: list[str],
    threshold: float,
    language: str,
    mode: str,
    token_state: dict,
) -> str:
    """Call POST /v1/detect on the NLP service and return the scrubbed text.

    Token state is sent with the request and updated in-place from the response
    so that deterministic surrogate tokens remain consistent across multiple
    fields of the same resource.

    Falls back to the original *text* unchanged (with a warning) if the NLP
    service is unreachable — processing continues rather than failing hard.
    """
    payload = json.dumps({
        "text": text,
        "entities": entities,
        "threshold": threshold,
        "language": language,
        "mode": mode,
        "token_state": token_state,
    }).encode("utf-8")

    url = _nlp_service_url("/v1/detect")
    req = _ureq.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _ureq.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        # Update token_state in-place so the caller's reference stays current
        returned_state = result.get("token_state", {})
        token_state.update(returned_state)
        return result["scrubbed_text"]
    except _uerr.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200] if exc.fp else ""
        _log.warning("nlp_service_http_error code=%s detail=%s — skipping NLP", exc.code, detail)
        return text
    except _uerr.URLError as exc:
        _log.warning("nlp_service_unreachable reason=%s — skipping NLP", exc.reason)
        return text
    except Exception as exc:
        _log.warning("nlp_service_error type=%s — skipping NLP", type(exc).__name__)
        return text
