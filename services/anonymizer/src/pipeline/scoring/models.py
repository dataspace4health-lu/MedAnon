"""Scoring engine data models - re-exported from the domain layer.

The dataclasses moved to ``domain.scoring`` (pure DTOs belong in the domain, so
adapters like ``integrations.scoring.client`` can depend on them without reaching
up into the pipeline). This module re-exports them so the many existing
``from pipeline.scoring.models import ...`` call sites keep working. New code
should import from ``domain.scoring`` directly.
"""

from __future__ import annotations

from domain.scoring import (  # noqa: F401
    SCORE_EXTENSION_URL,
    Evidence,
    ModuleScore,
    PrivacyDecision,
    ScoreResult,
)
