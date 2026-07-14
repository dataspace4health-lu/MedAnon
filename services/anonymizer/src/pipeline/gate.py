"""Output-gate primitives extracted from the orchestrator.

These three pieces are independent of the batch-orchestration state in
:mod:`pipeline.processor` and are imported widely (the API service/router
layer catches :class:`PiiLeakError`):

- :class:`PiiLeakError`        — raised when the raw-PII gate blocks output.
- :func:`run_pii_gate`         — the raw-resource PII scan half of the unified
  output barrier (see :mod:`pipeline.validation`).
- :func:`quarantine_info_for`  — PHI-free identity fields for a quarantine record.

``processor`` re-exports all three under their historical names
(``PiiLeakError``, ``_run_pii_gate``, ``_quarantine_info_for``) so existing
imports and patch targets keep working.
"""

from __future__ import annotations

import logging

from pipeline.exceptions import OutputBlocked
from utils.regulated import ner_gate_mode, raw_pii_scan_enabled

audit_log = logging.getLogger("medanon.audit")


class PiiLeakError(OutputBlocked):
    """Raised when the PII blocking gate detects blocking-severity PII in output.

    ``detections`` is already the filtered blocking set (see
    :func:`pii_detector.blocking_detections`); every entry tripped the gate.

    Subclasses :class:`~pipeline.exceptions.OutputBlocked` so a handler that
    catches the general barrier refusal also catches a raw-PII block.  Handlers
    that want the PII-specific 422 body must keep their ``except PiiLeakError``
    clause *before* any ``except OutputBlocked``.
    """

    def __init__(self, detections: list[dict]):
        self.detections = detections
        message = (
            f"PII gate blocked: {len(detections)} personal-identifier leak(s) "
            "detected in output"
        )
        super().__init__(message, reasons=[message])


def _source_blocks(detection: dict) -> bool:
    """Whether a detection's *source* is allowed to block release.

    ``regex`` and ``structural`` are deterministic and always block (subject to
    the severity filter).  ``ner`` is statistical: see
    :func:`utils.regulated.ner_gate_mode`.  ``ai`` never reaches this function —
    the blocking gate calls ``detect_pii_fast``, which runs no LLM layer.
    """
    if detection.get("source") != "ner":
        return True
    return ner_gate_mode() == "block"


def raw_detections(
    results: list[dict],
    manifest_entries_list: "list[list[dict]] | None" = None,
    settings=None,
) -> "tuple[list[dict], list[dict]]":
    """Scan de-identified *results*; return ``(blocking, advisory)`` detections.

    Two complementary checks, because neither alone is sufficient:

    - **Content scan** (``pii_detector.detect_pii_fast``) finds residual
      identifiers in free text.  It only walks strings of >= 15 characters, so a
      leaked ``name.family`` is invisible to it.
    - **Structural check** (``identifier_gate.structural_detections``) finds
      HIPAA-sensitive *paths* that are present in the output with no recorded
      transformation.  Deterministic, model-free, and exactly covers the
      structured fields the content scan cannot see.  Needs the manifest, so it
      no-ops when ``manifest_entries_list`` is absent.

    A detection blocks when its severity is in
    ``pii_detector.block_severities`` (``MEDANON_PII_GATE_BLOCK_SEVERITY``,
    default ``critical,high``) *and* its source is allowed to block (see
    :func:`_source_blocks`).  Everything else is advisory: counted, audit-logged,
    and released.

    Whether the scan runs at all is decided by
    :func:`utils.regulated.raw_pii_scan_enabled`, read at call time so operators
    and tests can toggle it without re-importing.
    """
    if not raw_pii_scan_enabled():
        return [], []

    # Filter quarantine records, keeping manifests index-aligned with resources.
    valid_resources: list[dict] = []
    valid_manifests: "list[list[dict]] | None" = (
        None if manifest_entries_list is None else []
    )
    for i, r in enumerate(results):
        if not isinstance(r, dict) or "error" in r:
            continue
        valid_resources.append(r)
        if valid_manifests is not None:
            valid_manifests.append(
                manifest_entries_list[i] if i < len(manifest_entries_list) else []
            )
    if not valid_resources:
        return [], []

    try:
        from integrations.ai.agents.pii_detector import (
            block_severities,
            detect_pii_fast,
        )
    except ImportError:
        # The detector is optional; absence must not crash the pipeline.
        audit_log.debug("pii_detector_unavailable — skipping raw PII scan")
        return [], []

    from pipeline.identifier_gate import structural_detections

    detections = detect_pii_fast(valid_resources)
    detections.extend(structural_detections(valid_resources, valid_manifests, settings))

    from utils.metrics import PHI_LEAK_DETECTED

    severities = block_severities()
    blocking: list[dict] = []
    advisory: list[dict] = []
    for d in detections:
        PHI_LEAK_DETECTED.labels(
            type=str(d.get("type", "unknown")),
            severity=str(d.get("severity", "unknown")),
        ).inc()
        if d.get("severity") in severities and _source_blocks(d):
            blocking.append(d)
        else:
            advisory.append(d)
    return blocking, advisory


def blocking_raw_detections(
    results: list[dict],
    manifest_entries_list: "list[list[dict]] | None" = None,
    settings=None,
) -> list[dict]:
    """The blocking half of :func:`raw_detections`.

    The **single** implementation both halves of the barrier call:
    :func:`run_pii_gate` (the ``process_data_batch`` choke point, which raises)
    and ``pipeline.validation._run_raw_pii_scan`` (the aggregate barrier used by
    the non-FHIR source adapters, which folds it into a verdict).  They
    previously carried separate copies of the enable/skip logic and disagreed:
    only ``validation``'s copy honoured regulated mode.
    """
    return raw_detections(results, manifest_entries_list, settings)[0]


def _log_advisory(advisory: list[dict]) -> None:
    """Audit-log advisory detections, grouped by source, without echoing PHI."""
    if not advisory:
        return
    by_source: dict[str, int] = {}
    for d in advisory:
        by_source[str(d.get("source", "unknown"))] = (
            by_source.get(str(d.get("source", "unknown")), 0) + 1
        )
    audit_log.warning(
        "pii_gate_advisory total=%d by_source=%s paths=%s",
        len(advisory),
        by_source,
        sorted({str(d.get("field_path", "?")) for d in advisory})[:10],
    )


def run_pii_gate(
    results: list[dict],
    manifest_entries_list: "list[list[dict]] | None" = None,
    settings=None,
) -> None:
    """Run the output barrier on de-identified results; raise on a blocking leak.

    The choke point every ``process_data_batch`` caller passes through (batch
    API, NDJSON streaming, async bulk/cohort jobs, staged worker, Bundle inner
    processing).  ON by default; ``MEDANON_OUTPUT_GATE_ENABLED=false`` disables
    the barrier and the legacy ``MEDANON_PII_GATE=false`` opts the raw scan out
    individually — neither is honoured in regulated mode.

    Pass *manifest_entries_list* (index-aligned with *results*) and *settings* to
    enable the structural coverage check; without them only the free-text
    content scan runs.
    """
    if not raw_pii_scan_enabled():
        return

    blocking, advisory = raw_detections(results, manifest_entries_list, settings)
    _log_advisory(advisory)

    from utils.metrics import GATE_DECISIONS

    if blocking:
        GATE_DECISIONS.labels(gate="raw_pii", decision="block").inc()
        audit_log.warning(
            "pii_gate_blocked blocking=%d advisory=%d", len(blocking), len(advisory)
        )
        raise PiiLeakError(blocking)
    GATE_DECISIONS.labels(gate="raw_pii", decision="pass").inc()


def quarantine_info_for(resource, exc: BaseException) -> dict:
    """PHI-free identity fields for a quarantine record.

    Captured at the match-stage catch site (where the resource and exception
    are still in scope) so the quarantine record emitted in finalize can be
    tied back to a source resource. The resource id is hashed — quarantine
    records travel in output streams and audit logs, so the raw id
    (potentially identifying) must not leak.
    """
    rtype = (
        resource.get("resourceType", "Unknown")
        if isinstance(resource, dict)
        else "Unknown"
    )
    rid = resource.get("id") if isinstance(resource, dict) else None
    hashed = None
    if rid:
        import hashlib

        hashed = "sha256:" + hashlib.sha256(str(rid).encode("utf-8")).hexdigest()[:16]
    return {
        "resource_type": rtype,
        "resource_id": hashed,
        "error_type": type(exc).__name__,
    }
