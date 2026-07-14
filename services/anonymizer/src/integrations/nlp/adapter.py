"""NLP detector adapter  delegates to the NLP microservice over HTTP."""

from __future__ import annotations

import logging
import os
import threading

_log = logging.getLogger("medanon.nlp.adapter")


class RemoteNlpAdapter:
    """Implements NlpDetectorPort via the NLP microservice over HTTP."""

    def detect(
        self,
        text: str,
        entities: list[str],
        threshold: float,
        language: str,
    ) -> list[tuple[int, int, str]]:
        from integrations.nlp.remote_detector import detect_remote

        return detect_remote(text, entities, threshold, language)

    def detect_batch(
        self,
        texts: list[str],
        entities: list[str],
        threshold: float,
        language: str,
    ) -> list[list[tuple[int, int, str]]]:
        from integrations.nlp.remote_detector import detect_batch_remote

        return detect_batch_remote(texts, entities, threshold, language)

    def analyze_and_replace(
        self,
        text: str,
        entities: list[str],
        threshold: float,
        language: str,
        mode: str,
        token_state: dict,
    ) -> str:
        from integrations.nlp.remote_detector import analyze_and_replace_remote

        return analyze_and_replace_remote(
            text, entities, threshold, language, mode, token_state
        )

    def analyze_and_replace_batch(
        self,
        texts: list[str],
        entities: list[str],
        threshold: float,
        language: str,
        mode: str,
        token_state: dict,
    ) -> list[str]:
        from integrations.nlp.remote_detector import analyze_and_replace_batch_remote

        return analyze_and_replace_batch_remote(
            texts, entities, threshold, language, mode, token_state
        )


# ---------------------------------------------------------------------------
# NLP adapter singleton accessor.
#
# Lives with the NLP integration (not pipeline.deidentify) so the adapter
# lifecycle is owned by the adapter package. Callers across the pipeline, the
# api layer, and other adapters import _get_nlp_adapter from here. Tests inject
# a mock by assigning `_nlp_adapter` on this module.
# ---------------------------------------------------------------------------

_nlp_adapter = None
_NLP_UNAVAILABLE = object()  # sentinel: tried to init and returned no adapter
_nlp_adapter_lock = threading.Lock()
_nlp_adapter_initialised = False


def _get_nlp_adapter():
    """Return the NLP detector adapter, creating it once on first use.

    Requires NLP_SERVICE_URL to be set: the NLP engine runs exclusively as the
    NLP microservice (RemoteNlpAdapter). Returns None if the env var is absent;
    callers must handle this gracefully.

    Thread-safe: the initialisation block runs once under a lock; the fast path
    (already-set, including test injection) never acquires the lock.
    """
    global _nlp_adapter, _nlp_adapter_initialised
    if _nlp_adapter is not None:
        return None if _nlp_adapter is _NLP_UNAVAILABLE else _nlp_adapter
    with _nlp_adapter_lock:
        if _nlp_adapter is not None:
            return None if _nlp_adapter is _NLP_UNAVAILABLE else _nlp_adapter
        nlp_url = os.environ.get("NLP_SERVICE_URL", "")
        if nlp_url:
            _nlp_adapter = RemoteNlpAdapter()
        else:
            _log.warning(
                "NLP_SERVICE_URL not set: NLP scrubbing unavailable. "
                "Set NLP_SERVICE_URL=http://nlp-lb:8200 to enable."
            )
            _nlp_adapter = _NLP_UNAVAILABLE
        _nlp_adapter_initialised = True
    return None if _nlp_adapter is _NLP_UNAVAILABLE else _nlp_adapter
