"""Permit context  request/job-scoped propagation of the active data permit.

TEHDAS2 D7.2 §4.4: pseudonyms MUST NOT be reused across different data
permits, and reversal of pseudonymisation may only be performed by the HDAB
or a designated TTP (Art 66(3))  never the data user. Threading a new
parameter through every action-function signature and every processing entry
point (sync ``/process``, streaming, staged workers, jobs) would be a large,
invasive refactor. Instead the active permit id  once validated for the
current request/job  is carried on a ``contextvars.ContextVar``, mirroring
the idiom :mod:`pipeline.trace` already uses for the correlation id, so it
propagates across thread-pool workers via the existing context-copy wrapper.

Keyed pseudonymisation actions (``cryptohash``, ``tokenize``, ``date_shift``)
and the gPAS orchestrator read :func:`get_permit_id` to scope their derived
keys / gPAS domains per permit. When :func:`utils.regulated.regulated_mode`
is on, those call sites require an active permit context and fail closed
(:class:`PermitRequiredError`) otherwise.
"""

from __future__ import annotations

import contextvars

_permit_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "medanon_permit_id", default=""
)


def set_permit_id(permit_id: str | None) -> contextvars.Token:
    """Set the active permit id; returns a token for :func:`reset_permit_id`."""
    return _permit_id.set(permit_id or "")


def reset_permit_id(token: contextvars.Token) -> None:
    """Restore the previous permit id (pair with :func:`set_permit_id`)."""
    try:
        _permit_id.reset(token)
    except (ValueError, LookupError):
        # Token from a different context (e.g. crossed a thread boundary)
        # best-effort reset; never raise from a context helper.
        pass


def get_permit_id() -> str:
    """Return the active permit id, or ``""`` when none is set."""
    return _permit_id.get()


class permit_scope:
    """Context manager that binds *permit_id* as the active permit for its body.

    Usage::

        with permit_scope(permit_id):
            process_data_batch(resources, settings, pseudonymizer)
    """

    def __init__(self, permit_id: str | None) -> None:
        self._permit_id = permit_id or ""
        self._token: contextvars.Token | None = None

    def __enter__(self) -> str:
        self._token = set_permit_id(self._permit_id)
        return self._permit_id

    def __exit__(self, *_exc) -> None:
        if self._token is not None:
            reset_permit_id(self._token)


class PermitRequiredError(ValueError):
    """Raised when regulated mode requires an active permit context but none is set."""


def require_permit_if_regulated(action: str) -> str:
    """Return the active permit id; fail closed in regulated mode when unset.

    Non-regulated deployments get back whatever is set (possibly ``""``
    legacy global-key/domain behaviour, unchanged for backward compatibility).
    Regulated deployments must bind every keyed pseudonymisation / gPAS call
    to a permit (D7.2 §4.4).
    """
    from utils.regulated import regulated_mode

    pid = get_permit_id()
    if not pid and regulated_mode():
        raise PermitRequiredError(
            f"MEDANON_REGULATED_MODE is on: {action} requires an active data "
            "permit context (D7.2 §4.4  pseudonyms and derived keys must be "
            "scoped per permit and MUST NOT be reused across permits). Pass "
            "permit_id on the request/job; it must resolve to an APPROVED, "
            "currently-active permit."
        )
    return pid


def scope_key_to_permit(base_key: str, *, action: str) -> str:
    """Return *base_key*, HKDF-derived per the active permit when one is set.

    Fails closed in regulated mode when no permit context is active (see
    :func:`require_permit_if_regulated`). Outside regulated mode, callers
    with no permit context get *base_key* back unchanged.
    """
    from utils.crypto import derive_permit_key, hash_key_id

    permit_id = require_permit_if_regulated(action)
    if not permit_id:
        return base_key
    return derive_permit_key(base_key, permit_id=permit_id, key_id=hash_key_id())


def scope_domain_to_permit(domain: str, *, action: str = "gpas") -> str:
    """Return *domain*, suffixed with the active permit id when one is set.

    Fails closed in regulated mode when no permit context is active. Domain
    suffixing (rather than key derivation) is used for gPAS because gPAS
    itself holds the pseudonymisation secrets  a distinct domain is gPAS's
    unit of pseudonym-space isolation (mirrors the D7.2 §4.6 "trusted third
    party" administration-service model).
    """
    permit_id = require_permit_if_regulated(action)
    if not permit_id:
        return domain
    return f"{domain}__permit-{permit_id}"
