"""Scoring service integration package.

When ``SCORING_SERVICE_URL`` is set, scoring is delegated to a remote
microservice. Otherwise the in-process engine in ``pipeline.scoring`` is used.

Public entry point: ``get_remote_scoring_client()`` returns a configured
``RemoteScoringClient`` or ``None`` when no URL is set.
"""

from integrations.scoring.client import (  # noqa: F401
    RemoteScoringClient,
    get_remote_scoring_client,
)
