"""NLP detector adapters — implementations of NlpDetectorPort.

Provides local (Presidio) and remote (HTTP microservice) adapters that
decouple the pipeline from the NLP infrastructure.
"""

from __future__ import annotations


class LocalPresidioAdapter:
    """Implements NlpDetectorPort via the local Presidio engine."""

    def analyze_and_replace(
        self,
        text: str,
        entities: list[str],
        threshold: float,
        language: str,
        mode: str,
        token_state: dict,
    ) -> str:
        from integrations.nlp.detector import _analyze_and_replace

        return _analyze_and_replace(
            text, entities, threshold, language, mode, token_state
        )


class RemoteNlpAdapter:
    """Implements NlpDetectorPort via the NLP microservice over HTTP."""

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
