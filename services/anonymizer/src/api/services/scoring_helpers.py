"""Scoring helper utilities for processing endpoints.

Shared by ``api/routers/process.py`` and ``api/routers/fhir_server.py``
to score de-identified results and persist processing runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone

from utils.json_fast import loads as _json_loads
from pipeline.manifest import extract_manifest_entries as _extract_manifest_entries

logger = logging.getLogger("medanon.scoring_helpers")

# Manifest system constant (same as pipeline/manifest.py)
_MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest"


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
    config_hash: str | None = None,
    trust_passport: dict | None = None,
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
        "config_hash": config_hash,
        "resource_count": resource_count,
        "error_count": error_count,
        "duration_ms": duration_ms,
        "input_type": input_type,
        "summary": summary,
        "score": score,
        "trust_passport": trust_passport,
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
    config_hash: str | None = None,
    trust_passport: dict | None = None,
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
        "config_hash": config_hash,
        "resource_count": resource_count,
        "error_count": error_count,
        "duration_ms": duration_ms,
        "input_type": input_type,
        "summary": summary,
        "score": score,
        "trust_passport": trust_passport,
    }
    try:
        await asyncio.to_thread(store.create, run)
    except Exception:
        logger.debug("processing_run_persist_failed", exc_info=True)


def apply_pii_leak_override(score: dict) -> "dict | None":
    """Detect a PII leak in *score*, override the score in-place if one is found.

    Returns the pii_leak info dict (same shape as ``extract_pii_leak_info``)
    when a leak is detected, or ``None`` when the output is clean.

    Mutates *score* directly so the persisted processing-run record reflects the
    leak — privacy is forced to failed and composite to 0.  This is called by
    both the non-streaming ``check_and_persist_with_leak`` path and the
    streaming ``/process/ndjson`` + ``/process/batch`` generators (which cannot
    block output after it has already been sent, but can embed the leak signal
    in the stream trailer for client-side display).
    """
    pii_leak = extract_pii_leak_info(score)
    if not pii_leak:
        return None
    if not pii_leak.get("leaked"):
        # Warning-only verdict (weak config, no detected PII): surface it in the
        # score/trailer but do NOT zero the score or withhold output.
        score["pii_leak_warning"] = True
        return pii_leak
    if isinstance(score.get("batch_privacy"), dict):
        score["batch_privacy"]["passed"] = False
        score["batch_privacy"]["risk_score"] = 1.0
        score["batch_privacy"]["pii_leak_override"] = True
    else:
        score["batch_privacy"] = {
            "passed": False,
            "risk_score": 1.0,
            "threshold": 0.3,
            "pii_leak_override": True,
        }
    total = (score.get("pass_count") or 0) + (score.get("fail_count") or 0)
    score["avg_composite"] = 0.0
    score["min_composite"] = 0.0
    score["pass_count"] = 0
    score["fail_count"] = total or score.get("total_scored", 1)
    score["pii_leak_blocked"] = True
    return pii_leak


def _identifier_coverage_blocks() -> bool:
    """Whether a HIPAA-coverage gap (identifier_risk) hard-blocks output.

    ``text_risk`` is *detected* PII in the output text (a real leak) and always
    blocks.  ``identifier_risk`` is a *coverage* signal — a HIPAA-sensitive field
    is present but no rule named it, so its value passes through unchanged.  In
    strict mode (default) that is also a hard block.  Operators who want a weak
    config to *warn* rather than block — surfacing the gap without withholding
    output — set ``MEDANON_GATE_IDENTIFIER_MODE=warn``.

        block (default) — uncovered HIPAA fields block output (current behaviour)
        warn            — uncovered HIPAA fields warn only; only detected PII
                          (text_risk) in the output blocks
    """
    mode = os.environ.get("MEDANON_GATE_IDENTIFIER_MODE", "block").strip().lower()
    return mode != "warn"


def extract_pii_leak_info(score: dict | None) -> "dict | None":
    """Return a verdict block when identifier or text risk hits are > 0, else None.

    The returned dict distinguishes a *blocking* leak from a *non-blocking*
    warning via the ``leaked`` flag:

      - ``text_risk_hits`` > 0  → real PII detected in output → ``leaked=True``.
      - ``identifier_risk_hits`` > 0 → HIPAA field present but uncovered by any
        rule. Blocks (``leaked=True``) in strict mode; warns (``leaked=False``,
        ``warning=True``) when ``MEDANON_GATE_IDENTIFIER_MODE=warn``.

    Callers hard-block only when ``leaked`` is True; a warning-only verdict is
    surfaced in the score/stream trailer for display but does not withhold output.
    """
    if not score or not score.get("computed"):
        return None
    id_hits = score.get("identifier_risk_hits", 0) or 0
    txt_hits = score.get("text_risk_hits", 0) or 0
    if id_hits == 0 and txt_hits == 0:
        return None

    id_blocks = id_hits > 0 and _identifier_coverage_blocks()
    leaked = txt_hits > 0 or id_blocks

    msgs: list[str] = []
    if txt_hits > 0:
        msgs.append(
            f"{txt_hits} resource(s) contain PII patterns in free-text fields "
            "not scrubbed by an NLP rule (phone, email, SSN, dates)"
        )
    if id_hits > 0:
        # Phrase the coverage gap as a leak when it blocks, as a warning when it
        # only warns — the wording drives what the UI shows the operator.
        if id_blocks:
            msgs.append(
                f"{id_hits} resource(s) still contain HIPAA-sensitive fields "
                "(Patient.name, identifier, birthDate, address, telecom) "
                "that are NOT covered by any de-identification rule"
            )
        else:
            msgs.append(
                f"Weak config: {id_hits} resource(s) have HIPAA-sensitive fields "
                "(Patient.name, identifier, birthDate, address, telecom) not "
                "covered by any rule. Output was released because no actual PII "
                "pattern was detected in the data, but coverage is incomplete"
            )

    return {
        "leaked": leaked,
        "warning": not leaked,
        "identifier_risk_hits": id_hits,
        "text_risk_hits": txt_hits,
        "resources_affected": max(id_hits, txt_hits),
        "message": "; ".join(msgs),
        "remediation": (
            f"Open config profile '{score.get('config_profile', '?')}' and add rules "
            "for every HIPAA-sensitive path. Run the Audit Report tab for the exact "
            "uncovered paths."
        ),
    }


async def check_and_persist_with_leak(
    result,
    endpoint: str,
    settings,
    t0: float,
    trust_passport: dict | None = None,
) -> "dict | None":
    """Score synchronously (awaited), persist the run, and return the pii_leak block.

    Unlike score_and_persist (fire-and-forget), this awaits scoring so the
    caller can inspect the leak signal BEFORE returning the HTTP response.

    ``trust_passport`` (the pre-privacy Trust Gate verdict, if any) is persisted
    with the run so every gated ingest path keeps its Quality Passport.
    """
    if not _is_scoring_enabled():
        return None
    try:
        config_profile = _get_config_profile(settings)
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

        if audit_report:
            score["audit_report"] = audit_report

        await persist_run(
            run_id=run_id,
            endpoint=endpoint,
            config_profile=config_profile,
            config_hash=getattr(settings, "config_hash", None),
            resource_count=summary.get("total_resources", 0),
            error_count=summary.get("error_count", 0),
            duration_ms=duration_ms,
            input_type=input_type,
            summary=summary,
            score=score,
            trust_passport=trust_passport,
        )
        pii_leak_info = apply_pii_leak_override(score)
        return pii_leak_info
    except Exception:
        logger.debug("check_and_persist_with_leak_failed", exc_info=True)
        return None


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

        if audit_report:
            score["audit_report"] = audit_report

        await persist_run(
            run_id=run_id,
            endpoint=endpoint,
            config_profile=config_profile,
            config_hash=getattr(settings, "config_hash", None),
            resource_count=summary.get("total_resources", 0),
            error_count=summary.get("error_count", 0),
            duration_ms=duration_ms,
            input_type=input_type,
            summary=summary,
            score=score,
        )
    except Exception:
        logger.debug("score_and_persist_failed", exc_info=True)
