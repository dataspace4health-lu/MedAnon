"""NLP detector adapters — implementations of NlpDetectorPort.

Provides local (Presidio) and remote (HTTP microservice) adapters that
decouple the pipeline from the NLP infrastructure.
"""

from __future__ import annotations


class LocalPresidioAdapter:
    """Implements NlpDetectorPort via the local Presidio engine."""

    def detect(
        self,
        text: str,
        entities: list[str],
        threshold: float,
        language: str,
    ) -> list[tuple[int, int, str]]:
        """Return raw entity detections without replacement."""
        from integrations.nlp.detector import _detect_entities_cached

        return _detect_entities_cached(text, tuple(entities), threshold, language)

    def detect_batch(
        self,
        texts: list[str],
        entities: list[str],
        threshold: float,
        language: str,
    ) -> list[list[tuple[int, int, str]]]:
        """Batch entity detection — pre-warms the detection cache."""
        from integrations.nlp.detector import _detect_entities_cached

        return [
            _detect_entities_cached(text, tuple(entities), threshold, language)
            for text in texts
        ]

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

    def analyze_and_replace_batch(
        self,
        texts: list[str],
        entities: list[str],
        threshold: float,
        language: str,
        mode: str,
        token_state: dict,
    ) -> list[str]:
        """Batch analyze and replace — sequential for token_state consistency."""
        from integrations.nlp.detector import _analyze_and_replace

        return [
            _analyze_and_replace(text, entities, threshold, language, mode, token_state)
            for text in texts
        ]


class RemoteNlpAdapter:
    """Implements NlpDetectorPort via the NLP microservice over HTTP."""

    def detect(
        self,
        text: str,
        entities: list[str],
        threshold: float,
        language: str,
    ) -> list[tuple[int, int, str]]:
        """Return raw entity detections without replacement."""
        from integrations.nlp.remote_detector import detect_remote

        return detect_remote(text, entities, threshold, language)

    def detect_batch(
        self,
        texts: list[str],
        entities: list[str],
        threshold: float,
        language: str,
    ) -> list[list[tuple[int, int, str]]]:
        """Batch entity detection via the NLP microservice."""
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
