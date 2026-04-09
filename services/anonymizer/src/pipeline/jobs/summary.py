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

# Manifest system constant — duplicated here to avoid circular import
_MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest"


def _extract_manifest_entries(resource: dict) -> list[dict]:
    """Extract transformation manifest entries from resource meta.tag."""
    meta = resource.get("meta")
    if not meta or not isinstance(meta, dict):
        return []
    tags = meta.get("tag", [])
    for tag in tags:
        if isinstance(tag, dict) and tag.get("system") == _MANIFEST_SYSTEM:
            display = tag.get("display", "")
            if display:
                try:
                    from utils.json_fast import loads as _json_loads
                    entries = _json_loads(display)
                    if isinstance(entries, list):
                        return entries
                except (ValueError, TypeError):
                    pass
    return []


class JobSummaryCollector:
    """Mutable accumulator used during job execution to track summary stats."""

    __slots__ = ("_type_counts", "_error_count", "_config_profile", "_started_at", "_score_collector")

    def __init__(self, config_profile: str = "auto") -> None:
        self._type_counts: Counter[str] = Counter()
        self._error_count: int = 0
        self._config_profile = config_profile
        self._started_at: float = time.monotonic()
        self._score_collector = None
        # Try to initialize scoring (may be disabled via env)
        try:
            from pipeline.scoring.constants import SCORING_ENABLED
            if SCORING_ENABLED:
                from pipeline.scoring.engine import ScoreCollector
                self._score_collector = ScoreCollector(config_profile=config_profile)
        except Exception:
            _log.debug("scoring_init_skipped", exc_info=True)

    def record_resource(self, resource: dict) -> None:
        """Record a processed resource (success or error marker)."""
        if not isinstance(resource, dict):
            return
        if "error" in resource:
            self._error_count += 1
            if self._score_collector is not None:
                self._score_collector.record_error()
            return
        rtype = resource.get("resourceType", "Unknown")
        self._type_counts[rtype] += 1
        # Score the resource
        if self._score_collector is not None:
            try:
                manifest_entries = _extract_manifest_entries(resource)
                self._score_collector.record_resource(
                    original=None,
                    deidentified=resource,
                    manifest_entries=manifest_entries,
                    settings=None,
                )
            except Exception:
                _log.debug("scoring_resource_error rtype=%s", rtype, exc_info=True)

    def record_error(self, resource_type: str = "Unknown") -> None:
        """Record a processing error."""
        self._error_count += 1
        if self._score_collector is not None:
            self._score_collector.record_error()

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
