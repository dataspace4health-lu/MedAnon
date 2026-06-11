"""Lightweight job completion summary collector.

Accumulates resource type counts, error counts, and de-identification
quality scores during processing, then produces a summary dict for
embedding in checkpoint_data.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime, timezone

_log = logging.getLogger("medanon.summary")

# Re-export the canonical extractor (single source of truth in pipeline.manifest).
from pipeline.manifest import (  # noqa: E402
    extract_manifest_entries as _extract_manifest_entries,
)


class JobSummaryCollector:
    """Mutable accumulator used during job execution to track summary stats."""

    __slots__ = (
        "_type_counts",
        "_error_count",
        "_config_profile",
        "_settings",
        "_job_id",
        "_started_at",
        "_score_collector",
    )

    def __init__(
        self,
        config_profile: str = "auto",
        settings=None,
        job_id: str = "",
    ) -> None:
        self._type_counts: Counter[str] = Counter()
        self._error_count: int = 0
        self._config_profile = config_profile
        self._settings = settings
        self._job_id = job_id
        self._started_at: float = time.monotonic()
        self._score_collector = None
        # Try to initialize scoring (may be disabled via env)
        try:
            from pipeline.scoring.constants import SCORING_ENABLED

            if SCORING_ENABLED:
                from pipeline.scoring.audit import ScoreAuditCollector

                self._score_collector = ScoreAuditCollector(
                    config_profile=config_profile,
                    job_id=job_id,
                )
        except Exception:
            _log.debug("scoring_init_skipped", exc_info=True)

    def record_resource(
        self,
        resource: dict,
        manifest_entries: "list[dict] | None" = None,
    ) -> None:
        """Record a processed resource (success or error marker).

        Args:
            resource:         The processed FHIR resource dict.
            manifest_entries: Pre-parsed manifest entries from the processor.
                              When provided, skips the JSON re-parse from
                              ``meta.tag`` — eliminates one ``json.loads()``
                              call per resource in the bulk-export hot loop.
        """
        if not isinstance(resource, dict):
            return
        if "error" in resource:
            self._error_count += 1
            if self._score_collector is not None:
                self._score_collector.record_error()
            return
        rtype = resource.get("resourceType", "Unknown")
        self._type_counts[rtype] += 1
        # Score the resource — use pre-parsed entries when the caller supplies
        # them to avoid re-parsing the JSON-encoded manifest from meta.tag.
        if self._score_collector is not None:
            try:
                if manifest_entries is None:
                    manifest_entries = _extract_manifest_entries(resource)
                self._score_collector.record_resource(
                    original=None,
                    deidentified=resource,
                    manifest_entries=manifest_entries,
                    settings=self._settings,
                )
            except Exception:
                _log.debug("scoring_resource_error rtype=%s", rtype, exc_info=True)

    def record_error(self, resource_type: str = "Unknown") -> None:
        """Record a processing error."""
        self._error_count += 1
        if self._score_collector is not None:
            self._score_collector.record_error()

    def generate_audit_report(self, export_meta: dict | None = None) -> str | None:
        """Generate the Markdown audit report; returns None when scoring is disabled."""
        if self._score_collector is None:
            return None
        try:
            return self._score_collector.generate_report(
                settings=self._settings,
                export_meta=export_meta or {},
            )
        except Exception:
            _log.debug("scoring_audit_report_error", exc_info=True)
            return None

    def to_dict(self, file_size_bytes: int = 0, compressed: bool = False) -> dict:
        """Produce the summary dict for checkpoint_data."""
        now = datetime.now(timezone.utc)
        elapsed = time.monotonic() - self._started_at
        result = {
            "total_resources": sum(self._type_counts.values()) + self._error_count,
            "error_count": self._error_count,
            "resource_type_counts": dict(self._type_counts),
            "config_profile": self._config_profile,
            "completed_at": now.isoformat(),
            "duration_sec": round(elapsed, 1),
            "compressed": compressed,
            "file_size_bytes": file_size_bytes,
        }
        # Include scoring aggregate when available
        if self._score_collector is not None:
            try:
                result["score"] = self._score_collector.aggregate()
            except Exception:
                _log.debug("scoring_aggregate_error", exc_info=True)
        return result
