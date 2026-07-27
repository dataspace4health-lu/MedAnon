"""Scoring service  on-demand job scoring + ad-hoc single-resource scoring.

Scoring is triggered manually by the user via POST /v1/jobs/{id}/score.
The service reads the job's NDJSON result file, extracts manifest entries
from meta.tag, scores each resource, and returns an aggregate result.

After scoring completes a Markdown audit report is stored under the job's
``score["audit_report"]`` explaining why each score is what it is and what
rules to add or change to improve it. It is served by
``GET /v1/jobs/{id}/score/report`` and never written to disk.
"""

from __future__ import annotations

import logging
from typing import Any

from utils.json_fast import loads as _json_loads
from scoring.engine import score_resource
from pipeline.scoring.audit import ScoreAuditCollector
from pipeline.manifest import extract_manifest_entries as _extract_manifest_entries

logger = logging.getLogger("medanon")


class ScoringService:
    """Stateless service for scoring de-identified FHIR resources."""

    @staticmethod
    def _load_settings(profile: str):
        """Load config settings for the given profile.

        Returns the settings object so that scoring evaluators (e.g. rule
        coverage) can inspect the active rules.  Returns None on failure so
        scoring can still proceed with reduced information.
        """
        try:
            from pipeline.config.service import get_settings

            return get_settings(profile)
        except Exception as exc:
            logger.debug(
                "scoring: could not load settings for profile=%s: %s", profile, exc
            )
            return None

    def score_resource(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        config_profile: str = "auto",
        settings: Any = None,
        include_audit: bool = False,
    ) -> dict:
        """Score a single resource and return the result as a dict.

        When ``include_audit=True`` the response includes an ``audit_report``
        field with the same Markdown report that bulk-export jobs produce,
        scoped to this single resource.
        """
        if include_audit:
            import datetime as _dt

            scored_at = (
                _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
            )
            collector = ScoreAuditCollector(
                config_profile=config_profile, scored_at=scored_at
            )
            result = collector.record_resource(
                original=original,
                deidentified=deidentified,
                manifest_entries=manifest_entries,
                settings=settings,
            )
            result_dict = result.to_dict()
            try:
                result_dict["audit_report"] = collector.generate_report(
                    settings=settings
                )
            except Exception as exc:
                logger.debug("score_audit_report_failed: %s", exc)
            return result_dict

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
        from domain.jobs import JobNotFound, JobNotComplete, JobStatus

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

        # Load config settings so scoring evaluators (rule_coverage etc.) can
        # inspect the active rules instead of returning indeterminate scores.
        settings = self._load_settings(profile)

        # Read NDJSON result file
        storage = get_result_storage()

        import datetime

        scored_at = (
            datetime.datetime.now(datetime.timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        collector = ScoreAuditCollector(
            config_profile=profile,
            job_id=job_id,
            scored_at=scored_at,
        )

        stream = storage.open_stream(job.result_path)
        try:
            for raw_line in stream:
                line = (
                    raw_line.decode("utf-8")
                    if isinstance(raw_line, (bytes, bytearray))
                    else raw_line
                )
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

                # Score with original=None (on-demand mode  utility uses
                # manifest-based estimation when originals are unavailable)
                collector.record_resource(
                    original=None,
                    deidentified=resource,
                    manifest_entries=manifest_entries,
                    settings=settings,
                )
        finally:
            if hasattr(stream, "close"):
                stream.close()

        score_summary = collector.aggregate()

        # The report rides inside `score` so it lands in the job store next to
        # the numbers it explains. Never written to /output: that directory is
        # reaped on MEDANON_RESULT_TTL_SEC, which would take the audit record
        # with the data it is supposed to outlive.
        from pipeline.scoring_helpers import attach_audit_report

        score_summary = attach_audit_report(
            score_summary,
            collector,
            settings=settings,
            export_meta={"fhir_source": (job.params or {}).get("source_url", "")},
        )

        # Cache the score (report included) in the job's checkpoint data
        try:
            checkpoint = job.checkpoint_data or {}
            checkpoint["score"] = score_summary
            save_checkpoint(store, job, checkpoint)
        except Exception as exc:
            logger.warning("score_cache_failed job=%s: %s", job_id, exc)

        return {
            "job_id": job_id,
            "computed": True,
            **score_summary,
        }

    def get_job_score(self, job_id: str) -> dict:
        """Return cached score summary for a job, or None if not yet scored."""
        import pipeline.jobs.store as _store_mod
        from domain.jobs import JobNotFound

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
                "reason": "scoring not yet triggered for this job  use POST /v1/jobs/{id}/score",
            }

        return {"job_id": job_id, "computed": True, **score}

    def get_audit_report(self, job_id: str) -> str:
        """Return the Markdown audit report content for a scored job.

        Read from the job's stored ``score``, where every executor now leaves it
        (nothing is written to ``/output``). Raises ``FileNotFoundError`` when
        the job has no stored report, ``JobNotFound`` when the job is unknown.
        """
        import pipeline.jobs.store as _store_mod
        from domain.jobs import JobNotFound

        store = _store_mod._job_store
        if store is None:
            raise RuntimeError("Job store not initialised")

        job = store.get(job_id)
        if job is None:
            raise JobNotFound()

        checkpoint = job.checkpoint_data or {}
        score = checkpoint.get("score") or (checkpoint.get("summary") or {}).get(
            "score"
        )
        report = (score or {}).get("audit_report")
        if not report:
            raise FileNotFoundError(
                f"No audit report stored for job {job_id}. "
                "Run POST /v1/jobs/{id}/score first."
            )
        return report
