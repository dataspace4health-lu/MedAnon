"""Trusted Third Party (TTP) integration seam (TEHDAS2 D7.2 §4.3/§4.4).

Defines the interface by which pseudonym reversal and cross-border linkage are
delegated to a designated TTP rather than held by the data holder. See
``docs/internal/ttp-cross-border-design.md`` for the design and scope decision.
"""

from integrations.ttp.provider import (
    LinkageRequest,
    LocalGpasTTP,
    RemoteTTP,
    ReversalRequest,
    TTPProvider,
    TTPUnavailableError,
    get_ttp_provider,
)

__all__ = [
    "LinkageRequest",
    "LocalGpasTTP",
    "RemoteTTP",
    "ReversalRequest",
    "TTPProvider",
    "TTPUnavailableError",
    "get_ttp_provider",
]
