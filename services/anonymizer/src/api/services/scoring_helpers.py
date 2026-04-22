"""Scoring helper utilities for processing endpoints.

Shared by ``api/routers/process.py`` and ``api/routers/fhir_server.py``
to score de-identified results and persist processing runs.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from utils.json_fast import loads as _json_loads

logger = logging.getLogger("medanon.scoring_helpers")

# Manifest system constant (same as pipeline/manifest.py)
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
                    entries = _json_loads(display)
                    if isinstance(entries, list):
                        return entries
                except (ValueError, TypeError):
                    pass
    return []


def _is_scoring_enabled() -> bool:
    """Check if scoring is enabled (cached at first call)."""
    try:
        from pipeline.scoring.constants import SCORING_ENABLED
        return SCORING_ENABLED
    except ImportError:
        return False


def _get_config_profile(settings) -> str:
    """Extract config profile name from settings object."""
    if hasattr(settings, "filename"):
        name = getattr(settings, "filename", "auto") or "auto"
        # Strip path and extension: config_hipaa_safe_harbor.yaml → hipaa
        if "/" in name:
            name = name.rsplit("/", 1)[-1]
        if name.startswith("config_"):
            name = name[7:]
        if name.endswith(".yaml"):
            name = name[:-5]
        return name
    return "auto"


def score_json_line(collector, line: str, settings=None) -> None:
    """Parse a JSON line and feed to the ScoreCollector.

    Silently catches all errors — scoring must never break the response stream.
    """
    if collector is None:
        return
    try:
        resource = _json_loads(line)
        if not isinstance(resource, dict):
            return
        if "__stream_complete" in resource:
            return  # trailer line — not a real resource
        if "error" in resource:
            collector.record_error()
            return
        manifest_entries = _extract_manifest_entries(resource)
        collector.record_resource(
            original=None,
            deidentified=resource,
            manifest_entries=manifest_entries,
            settings=settings,
        )
    except Exception:
        pass


def score_results(
    results,
    settings=None,
    config_profile: str = "auto",
) -> tuple[dict, dict, str | None]:
    """Score a result (dict, list, or Bundle) and return (summary, score, audit_report).

    Returns (summary_dict, score_dict, audit_report_markdown_or_None).
    """
    import datetime as _dt
    from pipeline.scoring.audit import ScoreAuditCollector

    scored_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    collector = ScoreAuditCollector(config_profile=config_profile, scored_at=scored_at)
    type_counts: Counter[str] = Counter()
    error_count = 0

    def _score_resource(resource: dict) -> None:
        nonlocal error_count
        if "error" in resource:
            error_count += 1
            collector.record_error()
            return
        rtype = resource.get("resourceType", "Unknown")
        type_counts[rtype] += 1
        manifest_entries = _extract_manifest_entries(resource)
        collector.record_resource(
            original=None,
            deidentified=resource,
            manifest_entries=manifest_entries,
            settings=settings,
        )

    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict):
                _score_resource(r)
    elif isinstance(results, dict):
        if results.get("resourceType") == "Bundle":
            for entry in results.get("entry", []):
                res = entry.get("resource") if isinstance(entry, dict) else None
                if isinstance(res, dict):
                    _score_resource(res)
        else:
            _score_resource(results)

    summary = {
        "resource_type_counts": dict(type_counts),
        "error_count": error_count,
        "total_resources": sum(type_counts.values()) + error_count,
    }
    score = collector.aggregate()
    try:
        audit_report = collector.generate_report(settings=settings)
    except Exception:
        audit_report = None
    return summary, score, audit_report


def make_collector(config_profile: str = "auto"):
    """Create a ScoreCollector if scoring is enabled, else return None."""
    if not _is_scoring_enabled():
        return None
    try:
        from pipeline.scoring.engine import ScoreCollector
        return ScoreCollector(config_profile=config_profile)
    except Exception:
        return None


def persist_run_sync(
    endpoint: str,
    config_profile: str,
    resource_count: int,
    error_count: int,
    duration_ms: int,
    input_type: str,
    summary: dict | None = None,
    score: dict | None = None,
    run_id: str | None = None,
) -> None:
    """Synchronous variant of persist_run for use inside worker threads."""
    from pipeline.processing_run import get_processing_run_store

    store = get_processing_run_store()
    if store is None:
        return

    run = {
        "id": run_id or str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "config_profile": config_profile,
        "resource_count": resource_count,
        "error_count": error_count,
        "duration_ms": duration_ms,
        "input_type": input_type,
        "summary": summary,
        "score": score,
    }
    try:
        store.create(run)
    except Exception:
        logger.debug("processing_run_persist_sync_failed", exc_info=True)


async def persist_run(
    endpoint: str,
    config_profile: str,
    resource_count: int,
    error_count: int,
    duration_ms: int,
    input_type: str,
    summary: dict | None = None,
    score: dict | None = None,
    run_id: str | None = None,
) -> None:
    """Persist a processing run to the database (fire-and-forget)."""
    from pipeline.processing_run import get_processing_run_store

    store = get_processing_run_store()
    if store is None:
        return

    run = {
        "id": run_id or str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "config_profile": config_profile,
        "resource_count": resource_count,
        "error_count": error_count,
        "duration_ms": duration_ms,
        "input_type": input_type,
        "summary": summary,
        "score": score,
    }
    try:
        await asyncio.to_thread(store.create, run)
    except Exception:
        logger.debug("processing_run_persist_failed", exc_info=True)


async def score_and_persist(
    result,
    endpoint: str,
    settings,
    t0: float,
) -> None:
    """Score a non-streaming result and persist the run.

    Called via ``asyncio.create_task()`` for fire-and-forget execution.
    """
    if not _is_scoring_enabled():
        return
    try:
        import os

        config_profile = _get_config_profile(settings)

        # Determine input type
        if isinstance(result, list):
            input_type = "array"
        elif isinstance(result, dict) and result.get("resourceType") == "Bundle":
            input_type = "Bundle"
        elif isinstance(result, dict):
            input_type = result.get("resourceType", "unknown")
        else:
            input_type = "unknown"

        run_id = str(uuid.uuid4())

        summary, score, audit_report = await asyncio.to_thread(
            score_results, result, settings, config_profile
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Write audit report to file alongside job result files
        if audit_report:
            try:
                output_dir = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
                audit_path = os.path.join(output_dir, f"{run_id}_score_audit.md")
                os.makedirs(output_dir, exist_ok=True)
                with open(audit_path, "w", encoding="utf-8") as fh:
                    fh.write(audit_report)
                score["audit_report_path"] = audit_path
            except Exception:
                logger.debug("score_audit_write_failed run=%s", run_id, exc_info=True)

        await persist_run(
            run_id=run_id,
            endpoint=endpoint,
            config_profile=config_profile,
            resource_count=summary.get("total_resources", 0),
            error_count=summary.get("error_count", 0),
            duration_ms=duration_ms,
            input_type=input_type,
            summary=summary,
            score=score,
        )
    except Exception:
        logger.debug("score_and_persist_failed", exc_info=True)
