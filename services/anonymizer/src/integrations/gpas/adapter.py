"""GpasPseudonymizerAdapter — production implementation of PseudonymizerPort.

Wraps the low-level ``gpas_pseudonymize_batch`` HTTP client so the domain
layer (processor, post_processor) can call pseudonymization without importing
from the HTTP integration layer directly.
"""

from __future__ import annotations

import os

from integrations.gpas.client import gpas_pseudonymize_batch
from integrations.gpas.transport import (
    _cache_get_many,
    _is_cache_enabled,
    _resolve_gpas_base,
)


class GpasPseudonymizerAdapter:
    """Implements :class:`~pipeline.ports.PseudonymizerPort` via the gPAS HTTP client."""

    def pseudonymize_batch(self, values: list[str], params: dict) -> dict[str, str]:
        """Delegate to the gPAS batch client.

        Args:
            values: Original string values to pseudonymize.
            params: gPAS parameters (domain, operation, auth, timeout, etc.).

        Returns:
            ``{original: pseudonym}`` mapping.
        """
        return gpas_pseudonymize_batch(values, params)

    def lookup_cache_batch(self, values: list[str], params: dict) -> dict[str, str]:
        """Return cached pseudonyms without making a gPAS HTTP call.

        Queries L1 (LocalLruCache) and, when Redis is configured, L2 (RedisCache).
        Values absent from both caches are not included in the result.

        Used by the supplement step in ``run_gpas_batch_for_batch`` so that
        excluded values that are still hot or warm in cache are resolved instantly
        (O(1) shard lock, no network I/O) without routing through the full
        ``pseudonymize_batch`` path.
        """
        if not values or not _is_cache_enabled(params):
            return {}
        base_url = _resolve_gpas_base(params)
        domain = params.get("gpas_domain") or os.environ.get("GPAS_DOMAIN", "")
        operation = params.get("gpas_operation", "pseudonymizeAllowCreate")
        cache_keys = [
            ("pseudonymize", base_url, domain, operation, str(v)) for v in values
        ]
        raw = _cache_get_many(cache_keys)
        return {str(v): raw[k] for v, k in zip(values, cache_keys) if k in raw}
