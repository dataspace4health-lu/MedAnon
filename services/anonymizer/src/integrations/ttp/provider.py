"""TTP provider interface + fail-closed default (TEHDAS2 D7.2 §4.3/§4.4, Art 66(3)).

EHDS restricts pseudonym *reversal* to the HDAB or a designated Trusted Third
Party, and foresees cross-border record **linkage** run through TTP/SPIDER-style
services rather than by exchanging identifiers. This module defines the seam for
both, following the strangler-fig pattern used elsewhere: a provider is selected
by ``MEDANON_TTP_PROVIDER`` and everything else degrades **fail-closed**  an
unconfigured or remote-only operation raises :class:`TTPUnavailableError` instead
of silently doing a local, unauthorised reversal or assuming linkage exists.

Providers
---------
- ``local`` (default): :class:`LocalGpasTTP`. The current single-holder model
  gPAS holds the mapping and reversal is an admin/HDAB-gated ``decrypt`` /
  ``gpas_depseudonymize`` action (see ``pipeline/config/reversal.py``). Cross-
  border linkage is **not** supported and fails closed.
- ``remote``: a designated remote TTP/SPIDER broker (integration point, not
  implemented here). Selecting it without ``MEDANON_TTP_URL`` fails closed so no
  data path assumes reversal/linkage exists.

The full multi-holder / secure-MPC design and the reasons it is scoped as an
interface rather than an in-tool implementation are in
``docs/internal/ttp-cross-border-design.md``.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class TTPUnavailableError(RuntimeError):
    """Raised (fail-closed) when a TTP operation is not available/authorised."""


@dataclass
class ReversalRequest:
    """A request to reverse pseudonyms back to identifiers, via the TTP."""

    pseudonyms: list[str] = field(default_factory=list)
    permit_id: str | None = None
    operator: str = "analyst"
    justification: str = ""


@dataclass
class LinkageRequest:
    """A cross-border linkage request: match subjects across holders without
    exchanging direct identifiers (TTP/SPIDER-mediated)."""

    local_pseudonyms: list[str] = field(default_factory=list)
    target_authority: str = ""
    permit_id: str | None = None


class TTPProvider(ABC):
    """Contract for delegating reversal + cross-border linkage to a TTP."""

    name: str = "abstract"

    @abstractmethod
    def supports_reversal(self) -> bool:
        """Whether this provider can reverse pseudonyms at all."""

    @abstractmethod
    def supports_cross_border(self) -> bool:
        """Whether this provider can mediate cross-border linkage."""

    @abstractmethod
    def reverse(self, request: ReversalRequest) -> dict[str, str]:
        """Return ``{pseudonym: identifier}``, or raise ``TTPUnavailableError``.

        Implementations MUST enforce that the caller is an HDAB/TTP operator
        (``admin``)  data users may never reverse (EHDS Art 66(3)).
        """

    @abstractmethod
    def link_cross_border(self, request: LinkageRequest) -> dict[str, Any]:
        """Return a linkage result, or raise ``TTPUnavailableError`` when the
        provider cannot mediate cross-border linkage."""


class LocalGpasTTP(TTPProvider):
    """Single-holder model: gPAS is the local TTP; no cross-border linkage.

    Reversal itself is executed by the admin/HDAB-gated ``gpas_depseudonymize`` /
    ``decrypt`` rule actions in the pipeline; this provider only encodes the
    policy (role gate) and the absence of cross-border capability.
    """

    name = "local"

    def supports_reversal(self) -> bool:
        return True

    def supports_cross_border(self) -> bool:
        return False

    def reverse(self, request: ReversalRequest) -> dict[str, str]:
        if request.operator != "admin":
            raise TTPUnavailableError(
                "reversal is restricted to the HDAB/TTP operator (admin); "
                f"operator '{request.operator}' may not reverse (EHDS Art 66(3))"
            )
        # The actual mapping lookup is performed by the gPAS-backed pipeline
        # action, not here  this provider is the policy/seam, not the store.
        raise TTPUnavailableError(
            "local reversal must run through the admin-gated gpas_depseudonymize "
            "pipeline action, not the TTP provider directly"
        )

    def link_cross_border(self, request: LinkageRequest) -> dict[str, Any]:
        raise TTPUnavailableError(
            "cross-border linkage requires a remote TTP broker; the local "
            "single-holder model does not support it (set MEDANON_TTP_PROVIDER=remote)"
        )


class RemoteTTP(TTPProvider):
    """Remote-TTP/SPIDER broker integration point (not yet implemented).

    Wiring target for a designated cross-border TTP service. Until implemented
    every operation fails closed so no caller assumes remote reversal/linkage
    exists. See ``docs/internal/ttp-cross-border-design.md``.
    """

    name = "remote"

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def supports_reversal(self) -> bool:
        return False

    def supports_cross_border(self) -> bool:
        return False

    def reverse(self, request: ReversalRequest) -> dict[str, str]:
        raise TTPUnavailableError(
            "remote TTP reversal is not implemented; see "
            "docs/internal/ttp-cross-border-design.md"
        )

    def link_cross_border(self, request: LinkageRequest) -> dict[str, Any]:
        raise TTPUnavailableError(
            "remote TTP cross-border linkage is not implemented; see "
            "docs/internal/ttp-cross-border-design.md"
        )


def get_ttp_provider() -> TTPProvider:
    """Select the TTP provider from ``MEDANON_TTP_PROVIDER`` (default ``local``).

    ``remote`` requires ``MEDANON_TTP_URL``; without a configured broker it fails
    closed rather than returning a non-functional provider.
    """
    provider = os.environ.get("MEDANON_TTP_PROVIDER", "local").strip().lower()
    if provider in ("", "local"):
        return LocalGpasTTP()
    if provider == "remote":
        endpoint = os.environ.get("MEDANON_TTP_URL", "").strip()
        if not endpoint:
            raise TTPUnavailableError(
                "MEDANON_TTP_PROVIDER=remote but no broker is configured "
                "(MEDANON_TTP_URL unset)  refusing to return a non-functional TTP"
            )
        return RemoteTTP(endpoint)
    raise TTPUnavailableError(f"unknown MEDANON_TTP_PROVIDER '{provider}'")
