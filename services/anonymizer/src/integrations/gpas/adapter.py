"""GpasPseudonymizerAdapter — production implementation of PseudonymizerPort.

Wraps the low-level ``gpas_pseudonymize_batch`` HTTP client so the domain
layer (processor, post_processor) can call pseudonymization without importing
from the HTTP integration layer directly.
"""

from __future__ import annotations

from integrations.gpas.client import gpas_pseudonymize_batch


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
