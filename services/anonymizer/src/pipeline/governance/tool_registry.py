"""Approved-tool / method registry (TEHDAS2 D7.2 §5.3, §6).

D7.2 expects de-identification, pseudonymisation and synthetic-generation to be
performed with *validated, approved* tools and methods, and for the choice to be
documented (it feeds the Transformation Passport's ``tools`` section). This is a
governance registry: it records which tool+version combinations are approved,
deprecated, or prohibited, with a pointer to the validation evidence, and lets a
release be checked against that policy.

Pure domain logic (no I/O); back it with any store. A tool registered with
``version=None`` approves *all* versions of that tool (use sparingly).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ToolStatus(str, Enum):
    APPROVED = "approved"
    DEPRECATED = "deprecated"
    PROHIBITED = "prohibited"
    UNKNOWN = "unknown"  # not in the registry


# Restrictiveness for aggregation (higher = worse for a release).
_STATUS_ORDER = {
    ToolStatus.APPROVED: 0,
    ToolStatus.DEPRECATED: 1,
    ToolStatus.UNKNOWN: 2,
    ToolStatus.PROHIBITED: 3,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class ApprovedTool:
    name: str
    version: str | None = None  # None = applies to any version
    category: str = ""  # e.g. pseudonymisation | anonymisation | synthetic
    status: ToolStatus = ToolStatus.APPROVED
    validation_ref: str = ""  # pointer to validation evidence (URL/ticket/doc)
    approved_by: str = ""
    approved_at: str = field(default_factory=_now_iso)

    def matches(self, name: str, version: str | None) -> bool:
        if self.name != name:
            return False
        return self.version is None or self.version == version


class ToolRegistry:
    """In-memory approved-tool registry."""

    def __init__(self) -> None:
        self._tools: list[ApprovedTool] = []

    def register(self, tool: ApprovedTool) -> ApprovedTool:
        self._tools.append(tool)
        return tool

    def status_of(self, name: str, version: str | None = None) -> ToolStatus:
        """Most specific match wins: an exact-version entry overrides a wildcard."""
        exact = [t for t in self._tools if t.name == name and t.version == version]
        if exact:
            return exact[0].status
        wildcard = [t for t in self._tools if t.name == name and t.version is None]
        if wildcard:
            return wildcard[0].status
        return ToolStatus.UNKNOWN

    def list(self) -> list[ApprovedTool]:
        return list(self._tools)


def default_tool_registry() -> ToolRegistry:
    """The baseline approved-tool registry (D7.2 §5.5.8).

    Seeds the engine itself as approved (any version). An HDAB extends this with
    the tools it has validated for its SPE; until then the passport's tool
    assessment simply confirms the run used the approved MedAnon engine.
    """
    reg = ToolRegistry()
    reg.register(
        ApprovedTool(
            name="medanon",
            version=None,  # wildcard  any engine version is approved
            category="de-identification",
            status=ToolStatus.APPROVED,
            validation_ref="built-in",
        )
    )
    return reg


def assess_tools(tools: list[dict], registry: ToolRegistry) -> dict[str, Any]:
    """Evaluate the tools used in a run against the registry.

    Args:
        tools: list of ``{"name":…, "version":…}`` (e.g. Passport ``tools``).
        registry: the approved-tool registry.

    Returns per-tool statuses, the list of not-approved tools, and an
    ``overall_status`` (the most restrictive across all tools).
    """
    results: list[dict[str, Any]] = []
    worst = ToolStatus.APPROVED
    for t in tools:
        name = t.get("name", "")
        version = t.get("version")
        status = registry.status_of(name, version)
        results.append({"name": name, "version": version, "status": status.value})
        if _STATUS_ORDER[status] > _STATUS_ORDER[worst]:
            worst = status
    return {
        "tools": results,
        "overall_status": worst.value,
        "not_approved": [
            r for r in results if r["status"] != ToolStatus.APPROVED.value
        ],
    }
