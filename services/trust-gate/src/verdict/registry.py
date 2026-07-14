"""Check registry (Strategy pattern).

Each entry is a *check strategy*: a callable that maps an ``AssessmentContext`` to a
list of ``CheckResult``. ``runner.run_checks`` iterates this registry instead of
hardcoding each check call, so the orchestrator no longer knows about individual
checks  adding a check to the FHIR suite means registering one adapter here (then
phase/dimension-tagging it in ``phases.py`` / ``dimensions.py``).

Each adapter is a thin translation from the context's fields to the check module's
own signature; the check modules themselves are unchanged.
"""

from __future__ import annotations

from typing import Callable

from checks import (
    accuracy,
    completeness,
    conformance,
    governance,
    identity,
    plausibility,
    timeliness,
)
from passport import CheckResult
from phases import STRUCTURAL, TERMINOLOGY

from verdict.context import AssessmentContext

# A check strategy: context in, check results out.
CheckStrategy = Callable[[AssessmentContext], list[CheckResult]]


def _conformance(ctx: AssessmentContext) -> list[CheckResult]:
    # external_validation=False (or a deselected phase) skips ONLY the slow external
    # FHIR-validator / terminology calls; the in-process structural checks still run.
    return conformance.evaluate(
        ctx.resources,
        ctx.validator,
        ctx.terminology_client,
        ctx.threshold_overrides,
        run_validator=ctx.external_validation and STRUCTURAL in ctx.selection,
        run_terminology=TERMINOLOGY in ctx.selection,
        full_urls=ctx.full_urls,
        ig_profiles=ctx.ig_profiles,
    )


def _completeness(ctx: AssessmentContext) -> list[CheckResult]:
    return completeness.evaluate(ctx.resources, ctx.threshold_overrides)


def _plausibility(ctx: AssessmentContext) -> list[CheckResult]:
    return plausibility.evaluate(
        ctx.resources,
        ctx.plausibility_rules or [],
        ctx.threshold_overrides,
        baseline_store=ctx.baseline_store,
        definitional_bounds=ctx.definitional_bounds,
        concordance_rules=ctx.concordance_rules,
        reference_time=ctx.reference_time,
    )


def _timeliness(ctx: AssessmentContext) -> list[CheckResult]:
    return timeliness.evaluate(
        ctx.resources, ctx.threshold_overrides, extraction_time=ctx.extraction_time
    )


def _accuracy(ctx: AssessmentContext) -> list[CheckResult]:
    return [
        accuracy.evaluate_against_reference(
            ctx.resources, ctx.reference, ctx.threshold_overrides
        )
    ]


def _identity(ctx: AssessmentContext) -> list[CheckResult]:
    return [identity.evaluate_patient_identity(ctx.resources, ctx.threshold_overrides)]


def _governance(ctx: AssessmentContext) -> list[CheckResult]:
    # Provenance SAM is inert (returns None) unless a provenance policy applies.
    chk = governance.evaluate_provenance_sam(ctx.resources, ctx.threshold_overrides)
    return [chk] if chk is not None else []


# Ordered so the SAM prerequisite conformance chain runs first (as before).
CHECK_REGISTRY: tuple[CheckStrategy, ...] = (
    _conformance,
    _completeness,
    _plausibility,
    _timeliness,
    _accuracy,
    _identity,
    _governance,
)
