r"""Data-permit domain model + lifecycle (TEHDAS2 D7.2 §2, EHDS Arts 45-49).

A *permit* is the legal authorisation that binds a data use: it names the
purpose, legal basis, controller, approved recipient, the scope of variables
allowed, and a validity window. Every SPE export decision (WS6) is checked
against an **active** permit, which is what makes pseudonyms and releases
*permit-scoped* (WS4).

This module is pure domain logic (no I/O): a state machine with validated
transitions plus dict (de)serialisation, so it is trivially testable and can be
backed by any store (in-memory for tests, Postgres in production  see
``pipeline.governance.store``).

Lifecycle::

    DRAFT --submit--> SUBMITTED --approve--> APPROVED --revoke--> REVOKED
                              \--reject--> REJECTED
    APPROVED is "active" only while now in [valid_from, valid_until];
    outside that window is_active() is False (treated as EXPIRED).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class PermitStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


# Allowed state transitions (target reached by the matching lifecycle method).
_TRANSITIONS: dict[PermitStatus, set[PermitStatus]] = {
    PermitStatus.DRAFT: {PermitStatus.SUBMITTED},
    PermitStatus.SUBMITTED: {PermitStatus.APPROVED, PermitStatus.REJECTED},
    PermitStatus.APPROVED: {PermitStatus.REVOKED},
    PermitStatus.REJECTED: set(),
    PermitStatus.REVOKED: set(),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    s = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class PermitTransitionError(ValueError):
    """Raised on an illegal permit lifecycle transition."""


@dataclass
class Permit:
    """A data-access permit."""

    id: str
    purpose: str = ""
    legal_basis: str = ""
    controller: str = ""
    recipient: str = ""
    # FHIRPaths (or path prefixes) the permit authorises access to.
    allowed_paths: list[str] = field(default_factory=list)
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    restrictions: list[str] = field(default_factory=list)
    status: PermitStatus = PermitStatus.DRAFT
    decided_by: str = ""
    decision_reason: str = ""
    created_at: datetime = field(default_factory=_now)

    # -- lifecycle ---------------------------------------------------------
    def _transition(self, target: PermitStatus, by: str = "", reason: str = "") -> None:
        if target not in _TRANSITIONS.get(self.status, set()):
            raise PermitTransitionError(
                f"cannot move permit from {self.status.value} to {target.value}"
            )
        self.status = target
        if by:
            self.decided_by = by
        if reason:
            self.decision_reason = reason

    def submit(self) -> None:
        self._transition(PermitStatus.SUBMITTED)

    def approve(self, by: str) -> None:
        self._transition(PermitStatus.APPROVED, by=by)

    def reject(self, by: str, reason: str = "") -> None:
        self._transition(PermitStatus.REJECTED, by=by, reason=reason)

    def revoke(self, by: str, reason: str = "") -> None:
        self._transition(PermitStatus.REVOKED, by=by, reason=reason)

    # -- queries -----------------------------------------------------------
    def is_active(self, at: datetime | None = None) -> bool:
        """True when the permit is APPROVED and *at* is within its window."""
        if self.status != PermitStatus.APPROVED:
            return False
        at = at or _now()
        if self.valid_from and at < self.valid_from:
            return False
        if self.valid_until and at > self.valid_until:
            return False
        return True

    def covers_path(self, path: str) -> bool:
        """True when *path* is within the permit's authorised scope.

        An empty ``allowed_paths`` means unrestricted (scope not enumerated).
        """
        if not self.allowed_paths:
            return True
        return any(path == a or path.startswith(a + ".") for a in self.allowed_paths)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        for k in ("valid_from", "valid_until", "created_at"):
            v = getattr(self, k)
            d[k] = (
                v.isoformat().replace("+00:00", "Z")
                if isinstance(v, datetime)
                else None
            )
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Permit":
        d = dict(data)
        status = d.get("status", PermitStatus.DRAFT.value)
        d["status"] = (
            PermitStatus(status) if not isinstance(status, PermitStatus) else status
        )
        for k in ("valid_from", "valid_until", "created_at"):
            if k in d:
                d[k] = _parse_dt(d[k])
        if d.get("created_at") is None:
            d["created_at"] = _now()
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in allowed})
