"""Scoring service — on-demand job scoring + ad-hoc single-resource scoring.

Scoring is triggered manually by the user via POST /v1/jobs/{id}/score.
The service reads the job's NDJSON result file, extracts manifest entries
from meta.tag, scores each resource, and returns an aggregate result.
"""

from __future__ import annotations

import logging
from typing import Any

from utils.json_fast import loads as _json_loads
from pipeline.scoring.engine import score_resource, ScoreCollector
from pipeline.scoring.models import ScoreResult
from pipeline.manifest import MANIFEST_SYSTEM

logger = logging.getLogger("medanon")


def _extract_manifest_entries(resource: dict) -> list[dict]:
    """Extract transformation manifest entries from resource meta.tag."""
    meta = resource.get("meta")
    if not meta or not isinstance(meta, dict):
        return []
    tags = meta.get("tag", [])
    for tag in tags:
        if isinstance(tag, dict) and tag.get("system") == MANIFEST_SYSTEM:
            display = tag.get("display", "")
            if display:
                try:
                    entries = _json_loads(display)
                    if isinstance(entries, list):
                        return entries
                except (ValueError, TypeError):
                    pass
    return []


class ScoringService:
    """Stateless service for scoring de-identified FHIR resources."""

    def score_resource(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        config_profile: str = "auto",
        settings: Any = None,
    ) -> dict:
        """Score a single resource and return the result as a dict."""
        result = score_resource(
            original=original,
            deidentified=deidentified,
            manifest_entries=manifest_entries,
            settings=settings,
            config_profile=config_profile,
        )
        return result.to_dict()

    def score_job(self, job_id: str, config_profile: str | None = None) -> dict:
        """Score a completed job on demand by reading its NDJSON result.

        Reads the job's result file, extracts manifest entries from each
        resource's meta.tag, scores each resource, and returns a batch-level
        aggregate score summary.

        The result is stored in the job's checkpoint_data["score"] for caching.
        """
        import pipeline.jobs.store as _store_mod
        from pipeline.jobs.checkpoint import save_checkpoint
        from integrations.storage import get_result_storage
        from medanon_core.domain import JobNotFound, JobNotComplete, JobStatus

        store = _store_mod._job_store
        if store is None:
            raise RuntimeError("Job store not initialised")

        job = store.get(job_id)
        if job is None:
            raise JobNotFound()

        if job.status != JobStatus.DONE:
            raise JobNotComplete()

        if not job.result_path:
            raise ValueError(f"Job {job_id} has no result file")

        # Determine config profile
        profile = config_profile or (job.params or {}).get("config_profile", "auto")

        # Read NDJSON result file
        storage = get_result_storage()
        collector = ScoreCollector(config_profile=profile)

        stream = storage.open_stream(job.result_path)
        try:
            for raw_line in stream:
                line = raw_line.decode("utf-8") if isinstance(raw_line, (bytes, bytearray)) else raw_line
                line = line.strip()
                if not line:
                    continue
                try:
                    resource = _json_loads(line)
                except (ValueError, TypeError):
                    collector.record_error()
                    continue

                if not isinstance(resource, dict) or "error" in resource:
                    collector.record_error()
                    continue

                # Extract manifest entries from meta.tag
                manifest_entries = _extract_manifest_entries(resource)

                # Score with original=None (on-demand mode — utility uses
                # manifest-based estimation fallback)
                collector.record_resource(
                    original=None,
                    deidentified=resource,
                    manifest_entries=manifest_entries,
                    settings=None,
                )
        finally:
            if hasattr(stream, "close"):
                stream.close()

        score_summary = collector.aggregate()

        # Cache the score in the job's checkpoint data
        try:
            checkpoint = job.checkpoint_data or {}
            checkpoint["score"] = score_summary
            save_checkpoint(store, job, checkpoint)
        except Exception as exc:
            logger.warning("score_cache_failed job=%s: %s", job_id, exc)

        return {"job_id": job_id, **score_summary}

    def get_job_score(self, job_id: str) -> dict:
        """Return cached score summary for a job, or None if not yet scored."""
        import pipeline.jobs.store as _store_mod
        from medanon_core.domain import JobNotFound

        store = _store_mod._job_store
        if store is None:
            raise RuntimeError("Job store not initialised")

        job = store.get(job_id)
        if job is None:
            raise JobNotFound()

        checkpoint = job.checkpoint_data or {}
        score = checkpoint.get("score")
        if score is None:
            summary = checkpoint.get("summary", {})
            score = summary.get("score")

        if score is None:
            return {
                "job_id": job_id,
                "computed": False,
                "reason": "scoring not yet triggered for this job — use POST /v1/jobs/{id}/score",
            }
        return {"job_id": job_id, **score}
