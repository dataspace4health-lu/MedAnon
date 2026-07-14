"""AssessmentContext — the immutable parameter object carrying every input a check
run needs. Replaces the long (14-arg) parameter list that used to be threaded through
``run_checks`` / ``targets_report`` / coverage. A per-sector re-run is a one-liner
(``ctx.scoped_to(subset)``) instead of re-passing thirteen keyword arguments.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from rules import PlausibilityRule
from terminology_client import TerminologyClient
from validator_client import ValidatorClient


@dataclass(frozen=True)
class AssessmentContext:
    """Immutable inputs for one assessment pass (or a scoped sub-pass).

    Frozen so a scoped copy (``scoped_to``) cannot accidentally mutate the parent;
    the checks read from it and never write to it.
    """

    resources: list[dict]
    selection: set[str]
    validator: ValidatorClient | None = None
    terminology_client: TerminologyClient | None = None
    threshold_overrides: dict[str, float] | None = None
    plausibility_rules: list[PlausibilityRule] | None = None
    baseline_store: object | None = None
    definitional_bounds: dict | None = None
    concordance_rules: list[dict] | None = None
    full_urls: list[str] | None = None
    extraction_time: str | None = None
    reference_time: str | None = None
    reference: dict | None = None
    ig_profiles: dict[str, list[str]] | None = None
    external_validation: bool = True

    def scoped_to(self, resources: list[dict]) -> "AssessmentContext":
        """A copy scoped to a resource subset (a per-sector re-run), reading the
        shared cross-batch baseline read-only (``baseline_store=None``) so the
        sub-pass cannot double-count into the accumulated reservoir."""
        return replace(self, resources=resources, baseline_store=None)
