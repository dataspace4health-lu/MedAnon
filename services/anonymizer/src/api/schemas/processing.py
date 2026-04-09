"""Pydantic models for dynamic processing settings (FHIR Parameters wrapper)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


@dataclass(frozen=True)
class RuntimeSettings:
    """Immutable runtime settings merged from config profile + dynamic overrides.

    Replaces the anonymous ``type('RuntimeSettings', ...)()`` object in deps.py.
    All pipeline code accesses these fields via ``getattr(settings, name, default)``.
    """

    rules: list = field(default_factory=list)
    processing_errors: str = "raise"
    rewrite_references: bool = False
    rewrite_text_ids: bool = False
    dynamic_rule_settings: dict = field(default_factory=dict)
    filename: str | None = None


class DynamicSettings(BaseModel):
    """Validated set of runtime overrides that can be passed via a FHIR Parameters wrapper.

    Replaces the ``_ALLOWED_DYNAMIC_SETTINGS`` frozenset + ``_validate_dynamic_settings()``
    function in api/deps.py.  Any unrecognised field is rejected (extra='forbid').
    """

    model_config = ConfigDict(extra="forbid")

    gpas_url: str | None = None
    gpas_domain: str | None = None
    gpas_operation: str | None = None
    gpas_token: str | None = None
    gpas_basic_user: str | None = None
    gpas_basic_pass: str | None = None
    gpas_timeout_sec: float | None = None
    gpas_retry_count: int | None = None
    processing_errors: str | None = None
    rewrite_references: bool | None = None
    rewrite_text_ids: bool | None = None


class DynamicSettingPart(BaseModel):
    """A single ``part`` entry inside a FHIR Parameters ``parameter`` block."""

    name: str
    valueString: str | None = None
    valueBoolean: bool | None = None
    valueDecimal: float | None = None
    valueInteger: int | None = None


class ParameterEntry(BaseModel):
    """A single ``parameter`` in a FHIR Parameters resource."""

    name: str
    resource: dict[str, Any] | None = None
    part: list[DynamicSettingPart] = []


class FhirParameters(BaseModel):
    """FHIR Parameters resource used as a wrapper to carry a resource + dynamic settings."""

    resourceType: Literal["Parameters"]
    parameter: list[ParameterEntry] = []
