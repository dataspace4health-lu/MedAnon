"""Discovery endpoints: the documented metric catalog and the use-case decision tree."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from metric_catalog import all_cards, use_cases

router = APIRouter()


@router.get("/v1/metric-catalog")
def metric_catalog() -> dict[str, Any]:
    """The documented metric cards (Phase 2): one per built-in check."""
    return {"cards": all_cards()}


@router.get("/v1/use-cases")
def use_case_profiles() -> dict[str, Any]:
    """The use-case decision-tree: declared use → metric subset (phases)."""
    return {"use_cases": use_cases()}
