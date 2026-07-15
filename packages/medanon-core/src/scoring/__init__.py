"""Scoring engine public API (medanon-core).

Usage::

    from scoring import score_resource, ScoreCollector, ScoreResult

    result = score_resource(original, deidentified, manifest_entries, settings)
    print(result.decision)   # "PASS" or "FAIL"
    print(result.composite)  # 0-100

This is the shared scoring engine, consumed both by the anonymizer monolith
(via the ``pipeline.scoring`` facade, which adds the audit collector) and by the
scoring microservice. It is a leaf: it depends only on ``domain`` and
``analytics``; the NLP adapter and remote scoring client are injected by the
host (see ``scoring.privacy.set_nlp_adapter_provider`` and
``scoring.engine.set_remote_client_provider``).
"""

from scoring.engine import (  # noqa: F401
    ScoreCollector,
    compute_composite,
    score_resource,
)
from scoring.models import (  # noqa: F401
    Evidence,
    ModuleScore,
    PrivacyDecision,
    ScoreResult,
)
from scoring.constants import SCORE_ATTACH, SCORING_ENABLED  # noqa: F401
