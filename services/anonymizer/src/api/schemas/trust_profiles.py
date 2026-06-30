"""Request schemas for Trust Gate audit-profile CRUD."""

from __future__ import annotations

from pydantic import BaseModel, field_validator

from pipeline.trust_profile import (
    PHASE_IDS,
    USE_CASE_IDS,
    validate_phases,
    validate_use_case,
)


def _check_phases(v: list[str] | None) -> list[str] | None:
    if v is None:
        return v
    if not v:
        raise ValueError("phases must be a non-empty list")
    invalid = validate_phases(v)
    if invalid:
        raise ValueError(
            f"unknown phase id(s): {', '.join(invalid)}. "
            f"Valid phases: {', '.join(PHASE_IDS)}"
        )
    return list(dict.fromkeys(v))  # dedupe, preserve order


def _check_use_case(v: str | None) -> str | None:
    if not validate_use_case(v):
        raise ValueError(
            f"unknown use_case '{v}'. Valid: {', '.join(USE_CASE_IDS)} (or empty)"
        )
    return v


class TrustProfileCreate(BaseModel):
    name: str
    description: str = ""
    phases: list[str]
    thresholds: dict | None = None
    targets: list | None = None
    intended_use: str = ""
    use_case: str = ""

    @field_validator("phases")
    @classmethod
    def _validate_phases(cls, v: list[str]) -> list[str]:
        return _check_phases(v)  # type: ignore[return-value]

    @field_validator("use_case")
    @classmethod
    def _validate_use_case(cls, v: str) -> str:
        return _check_use_case(v)  # type: ignore[return-value]


class TrustProfileUpdate(BaseModel):
    description: str | None = None
    phases: list[str] | None = None
    thresholds: dict | None = None
    targets: list | None = None
    intended_use: str | None = None
    use_case: str | None = None

    @field_validator("phases")
    @classmethod
    def _validate_phases(cls, v: list[str] | None) -> list[str] | None:
        return _check_phases(v)

    @field_validator("use_case")
    @classmethod
    def _validate_use_case(cls, v: str | None) -> str | None:
        return _check_use_case(v)
