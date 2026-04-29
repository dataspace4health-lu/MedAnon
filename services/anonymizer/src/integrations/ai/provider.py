"""LLM provider — lazy singleton with circuit breaker + response cache.

Uses litellm as the unified client.  When MEDANON_AI_ENABLED is false
all calls raise NotAvailableError.  When the circuit breaker is OPEN
calls raise ProviderUnavailableError.
"""

import logging
import os
import threading
import time
from typing import Any, Generator

from utils.circuit_breaker import CircuitBreaker

_log = logging.getLogger("medanon.ai")
_lock = threading.Lock()
_provider: "LLMProvider | None" = None


class ProviderUnavailableError(Exception):
    """Raised when the LLM provider is unreachable or circuit is OPEN."""


class NotAvailableError(Exception):
    """Raised when AI is disabled via MEDANON_AI_ENABLED=false."""


def is_ai_enabled() -> bool:
    return os.environ.get("MEDANON_AI_ENABLED", "false").strip().lower() in (
        "true", "1", "yes",
    )


def is_provider_reachable() -> bool:
    """Quick connectivity check (non-throwing)."""
    try:
        provider = get_provider()
        return provider._cb.allow_request()
    except (NotAvailableError, Exception):
        return False


class LLMProvider:
    """Thread-safe LLM provider with circuit breaker and optional caching."""

    def __init__(self) -> None:
        self._model = os.environ.get("MEDANON_AI_PROVIDER", "ollama/llama3.1")
        self._api_base = os.environ.get(
            "MEDANON_AI_API_BASE", "http://ollama:11434",
        )
        self._api_key = os.environ.get("MEDANON_AI_API_KEY", "")
        self._temperature = float(os.environ.get("MEDANON_AI_TEMPERATURE", "0.2"))
        self._max_tokens = int(os.environ.get("MEDANON_AI_MAX_TOKENS", "4096"))
        self._timeout = float(os.environ.get("MEDANON_AI_TIMEOUT_SEC", "60"))
        self._cb = CircuitBreaker(
            name="ai-provider",
            failure_threshold=int(
                os.environ.get("MEDANON_AI_CB_FAILURE_THRESHOLD", "3"),
            ),
            recovery_timeout_sec=float(
                os.environ.get("MEDANON_AI_CB_RECOVERY_TIMEOUT_SEC", "60"),
            ),
        )
        self._cache_enabled = os.environ.get(
            "MEDANON_AI_CACHE_ENABLED", "true",
        ).lower() in ("true", "1")
        self._cache_ttl = int(os.environ.get("MEDANON_AI_CACHE_TTL_SEC", "3600"))
        self._cache: dict[str, tuple[float, str]] = {}
        self._cache_lock = threading.Lock()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "api_base": self._api_base,
            "circuit_breaker": self._cb.stats,
            "cache_size": len(self._cache),
        }

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model_override: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cache_key: str | None = None,
    ) -> str:
        """Synchronous LLM completion with circuit breaker + cache."""
        if not self._cb.allow_request():
            raise ProviderUnavailableError("AI provider circuit breaker is OPEN")

        # Cache lookup
        if cache_key and self._cache_enabled:
            with self._cache_lock:
                entry = self._cache.get(cache_key)
                if entry and (time.monotonic() - entry[0]) < self._cache_ttl:
                    return entry[1]

        try:
            import litellm

            model = model_override or self._model
            api_base = None if model_override else self._api_base

            response = litellm.completion(
                model=model,
                messages=messages,
                api_base=api_base,
                api_key=self._api_key or None,
                temperature=(
                    temperature if temperature is not None else self._temperature
                ),
                max_tokens=max_tokens or self._max_tokens,
                timeout=self._timeout,
            )
            text = response.choices[0].message.content or ""
            self._cb.record_success()

            if cache_key and self._cache_enabled:
                with self._cache_lock:
                    self._cache[cache_key] = (time.monotonic(), text)

            return text
        except Exception as exc:
            self._cb.record_failure()
            _log.warning("ai_provider_error model=%s: %s", self._model, exc)
            raise ProviderUnavailableError(f"LLM call failed: {exc}") from exc

    def complete_streaming(
        self,
        messages: list[dict[str, str]],
        *,
        model_override: str | None = None,
        temperature: float | None = None,
    ) -> Generator[str, None, None]:
        """Generator yielding streaming chunks. For SSE endpoints."""
        if not self._cb.allow_request():
            raise ProviderUnavailableError("AI provider circuit breaker is OPEN")
        try:
            import litellm

            model = model_override or self._model
            api_base = None if model_override else self._api_base

            response = litellm.completion(
                model=model,
                messages=messages,
                api_base=api_base,
                api_key=self._api_key or None,
                temperature=(
                    temperature if temperature is not None else self._temperature
                ),
                max_tokens=self._max_tokens,
                timeout=self._timeout,
                stream=True,
            )
            for chunk in response:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception as exc:
            self._cb.record_failure()
            raise ProviderUnavailableError(
                f"LLM streaming failed: {exc}",
            ) from exc
        # Stream completed without exception — record exactly one success,
        # not one per chunk (would inflate the circuit breaker counter 10-100x).
        self._cb.record_success()

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._cache.clear()


def get_provider() -> LLMProvider:
    """Return the singleton LLMProvider. Raises NotAvailableError if disabled."""
    global _provider
    if not is_ai_enabled():
        raise NotAvailableError("AI is disabled (MEDANON_AI_ENABLED=false)")
    if _provider is not None:
        return _provider
    with _lock:
        if _provider is None:
            _provider = LLMProvider()
    return _provider


def reset_provider() -> None:
    """Reset the singleton (for tests)."""
    global _provider
    with _lock:
        _provider = None
