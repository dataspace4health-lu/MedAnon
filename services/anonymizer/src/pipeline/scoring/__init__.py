"""Scoring engine public API.

Usage::

    from pipeline.scoring import score_resource, ScoreCollector, ScoreResult

    result = score_resource(original, deidentified, manifest_entries, settings)
    print(result.decision)  # "PASS" or "FAIL"
    print(result.composite)  # 0-100
"""
from pipeline.scoring.engine import (  # noqa: F401
    ScoreCollector,
    compute_composite,
    score_resource,
)
from pipeline.scoring.models import (  # noqa: F401
    Evidence,
    ModuleScore,
    PrivacyDecision,
    ScoreResult,
)
from pipeline.scoring.constants import SCORING_ENABLED, SCORE_ATTACH  # noqa: F401
