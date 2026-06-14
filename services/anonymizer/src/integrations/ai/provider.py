"""LLM provider — lazy singleton with circuit breaker + response cache.

Uses litellm as the unified client.  When MEDANON_AI_ENABLED is false
all calls raise NotAvailableError.  When the circuit breaker is OPEN
calls raise ProviderUnavailableError.
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Generator

from integrations.ai.local_guard import (
    PiiModelNotLocalError,
    assert_endpoint_local,
    require_local,
)
from utils.circuit_breaker import CircuitBreaker

_log = logging.getLogger("medanon.ai")
_lock = threading.Lock()
_provider: "LLMProvider | None" = None


class ProviderUnavailableError(Exception):
    """Raised when the LLM provider is unreachable or circuit is OPEN."""


class _BoundedTtlCache:
    """Thread-safe LRU cache with TTL + proactive eviction on write.

    Replaces the previously unbounded ``dict`` response cache — entries were
    only dropped lazily on read, so unique cache keys accumulated forever.
    """

    def __init__(self, max_entries: int, ttl_sec: float) -> None:
        self._max = max(1, max_entries)
        self._ttl = ttl_sec
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()
        self.evictions = 0

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if (time.monotonic() - entry[0]) >= self._ttl:
                del self._data[key]
                self.evictions += 1
                return None
            self._data.move_to_end(key)
            return entry[1]

    def set(self, key: str, value: str) -> None:
        now = time.monotonic()
        with self._lock:
            # Proactive: drop expired entries from the LRU end first.
            while self._data:
                oldest_key = next(iter(self._data))
                if (now - self._data[oldest_key][0]) < self._ttl:
                    break
                del self._data[oldest_key]
                self.evictions += 1
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class NotAvailableError(Exception):
    """Raised when AI is disabled via MEDANON_AI_ENABLED=false."""


def is_ai_enabled() -> bool:
    return os.environ.get("MEDANON_AI_ENABLED", "false").strip().lower() in (
        "true",
        "1",
        "yes",
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
            "MEDANON_AI_API_BASE",
            "http://ollama:11434",
        )
        self._api_key = os.environ.get("MEDANON_AI_API_KEY", "")
        self._temperature = float(os.environ.get("MEDANON_AI_TEMPERATURE", "0.2"))
        self._max_tokens = int(os.environ.get("MEDANON_AI_MAX_TOKENS", "4096"))
        # Ollama context window. The few-shot config-generation prompt is
        # ~8.4k tokens; Ollama's default num_ctx is only 2048 (and even 8192
        # clips it), which silently truncates the prompt and yields empty
        # output. 16384 fits the full prompt plus the response budget.
        self._num_ctx = int(os.environ.get("MEDANON_AI_NUM_CTX", "16384"))
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
            "MEDANON_AI_CACHE_ENABLED",
            "true",
        ).lower() in ("true", "1")
        self._cache_ttl = int(os.environ.get("MEDANON_AI_CACHE_TTL_SEC", "3600"))
        self._cache = _BoundedTtlCache(
            max_entries=int(os.environ.get("MEDANON_AI_CACHE_MAX_ENTRIES", "256")),
            ttl_sec=self._cache_ttl,
        )

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "model": self._model,
            "api_base": self._api_base,
            "circuit_breaker": self._cb.stats,
            "cache_size": len(self._cache),
            "cache_evictions": self._cache.evictions,
        }

    def _enforce_local(
        self,
        model: str,
        api_base: str | None,
        phi_payload: bool,
    ) -> None:
        """C4 chokepoint: refuse non-local endpoints for PHI-bearing calls.

        Raised as ProviderUnavailableError so every existing caller's
        fallback path (static explain, heuristic config, skip-AI-layer)
        engages instead of surfacing a 500.
        """
        if not require_local(phi_payload):
            return
        try:
            assert_endpoint_local(model, api_base)
        except PiiModelNotLocalError as exc:
            _log.error(
                "ai_local_guard_blocked model=%s api_base=%s phi_payload=%s: %s",
                model,
                api_base,
                phi_payload,
                exc,
            )
            raise ProviderUnavailableError(str(exc)) from exc

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model_override: str | None = None,
        api_base_override: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        cache_key: str | None = None,
        phi_payload: bool = True,
    ) -> str:
        """Synchronous LLM completion with circuit breaker + cache.

        ``api_base_override`` pins a specific endpoint even when
        ``model_override`` is used — required by the local-only PII path so it
        can target a self-hosted model instead of litellm's default routing.

        ``phi_payload`` (fail-closed default True): when the messages may
        contain PHI/resource content, the effective endpoint must be provably
        local (C4). Callers handling only non-resource text (config intent,
        YAML rules) opt out with ``phi_payload=False`` — still enforced when
        MEDANON_AI_REQUIRE_LOCAL=true.
        """
        if not self._cb.allow_request():
            raise ProviderUnavailableError("AI provider circuit breaker is OPEN")

        model = model_override or self._model
        # A bare model_override (e.g. config-gen switching to gemma3:4b) still
        # targets the SAME local Ollama host, so the configured api_base must be
        # preserved — dropping it to None makes litellm fall back to its default
        # http://localhost:11434, which inside the container is nothing and fails
        # with "Connection refused". Only an explicit api_base_override changes
        # the endpoint. (Mirrors complete_streaming, which already did this.)
        if api_base_override is not None:
            api_base = api_base_override
        else:
            api_base = self._api_base

        # C4: enforce before any cache/network activity so misconfiguration
        # surfaces immediately and deterministically.
        self._enforce_local(model, api_base, phi_payload)

        # Cache lookup
        if cache_key and self._cache_enabled:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            import litellm

            response = litellm.completion(
                model=model,
                messages=messages,
                api_base=api_base,
                api_key=self._api_key or None,
                temperature=(
                    temperature if temperature is not None else self._temperature
                ),
                max_tokens=max_tokens or self._max_tokens,
                num_ctx=self._num_ctx,
                timeout=self._timeout,
            )
            text = response.choices[0].message.content or ""
            self._cb.record_success()

            if cache_key and self._cache_enabled:
                self._cache.set(cache_key, text)

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
        api_base_override: str | None = None,
        temperature: float | None = None,
        phi_payload: bool = True,
    ) -> Generator[str, None, None]:
        """Generator yielding streaming chunks. For SSE endpoints.

        ``api_base_override`` pins the endpoint; when omitted the configured
        ``MEDANON_AI_API_BASE`` is always used — including when only the model
        is overridden (model switches target the same local Ollama host, so the
        base must not be dropped to litellm's default).

        ``phi_payload`` semantics match :meth:`complete` — the streaming path
        previously had zero locality enforcement (C4).
        """
        if not self._cb.allow_request():
            raise ProviderUnavailableError("AI provider circuit breaker is OPEN")

        model = model_override or self._model
        api_base = (
            api_base_override if api_base_override is not None else self._api_base
        )
        self._enforce_local(model, api_base, phi_payload)

        try:
            import litellm

            response = litellm.completion(
                model=model,
                messages=messages,
                api_base=api_base,
                api_key=self._api_key or None,
                temperature=(
                    temperature if temperature is not None else self._temperature
                ),
                max_tokens=self._max_tokens,
                num_ctx=self._num_ctx,
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
