"""PseudonymizerPort — structural Protocol for pseudonymization adapters.

Any object implementing :meth:`pseudonymize_batch` satisfies this Protocol and
can be injected into :func:`~pipeline.processor.process_resource` or
:func:`~pipeline.processor.process_data_batch` in place of the default
:class:`~integrations.gpas.adapter.GpasPseudonymizerAdapter`.

Typical implementations:

* :class:`~integrations.gpas.adapter.GpasPseudonymizerAdapter` — production gPAS HTTP client
* Test stubs (pass a plain object or ``unittest.mock.MagicMock``)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PseudonymizerPort(Protocol):
    """Structural interface for pseudonymization backends.

    The protocol is intentionally minimal: a single synchronous method that
    accepts a list of plaintext values plus a parameter dict and returns a
    ``{original: pseudonym}`` mapping.  All error handling and retry logic
    live in the concrete implementation.
    """

    def pseudonymize_batch(
        self,
        values: list[str],
        params: dict,
    ) -> dict[str, str]:
        """Pseudonymize *values* in a single call.

        Args:
            values: Plaintext identifiers to pseudonymize (deduplicated by caller).
            params: Backend-specific parameters (e.g. ``{"domain": "...", "operation": "..."}``)

        Returns:
            Mapping ``{original_value: pseudonym}`` for every value in *values*.
            Values that cannot be pseudonymized may be absent from the mapping;
            callers must handle missing keys gracefully.
        """
        ...
