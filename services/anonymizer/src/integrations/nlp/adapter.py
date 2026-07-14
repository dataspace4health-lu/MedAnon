"""NLP detector adapter  delegates to the NLP microservice over HTTP."""

from __future__ import annotations


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
