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
import os

audit_log = logging.getLogger("medanon.audit")


class PiiLeakError(Exception):
    """Raised when the PII blocking gate detects critical PII in output."""

    def __init__(self, detections: list[dict]):
        self.detections = detections
        critical = [d for d in detections if d.get("severity") == "critical"]
        super().__init__(
            f"PII gate blocked: {len(critical)} critical PII leak(s) detected"
        )


def run_pii_gate(results: list[dict]) -> None:
    """Run fast PII scan on de-identified results and raise on critical PII.

    Part of the unified output-validation barrier (:mod:`pipeline.validation`):
    ON by default, disabled with ``MEDANON_OUTPUT_GATE_ENABLED=false``.  The
    legacy ``MEDANON_PII_GATE=false`` still opts the raw scan out individually.
    Both flags are read at call time so tests and operators can toggle them
    without re-importing the module.
    """
    from pipeline.validation import _gate_enabled

    if not _gate_enabled():
        return
    if os.environ.get("MEDANON_PII_GATE", "").strip().lower() in ("false", "0", "no"):
        return
    from integrations.ai.agents.pii_detector import detect_pii_fast

    valid_resources = [r for r in results if isinstance(r, dict) and "error" not in r]
    if not valid_resources:
        return
    detections = detect_pii_fast(valid_resources)
    critical = [d for d in detections if d.get("severity") == "critical"]
    if critical:
        audit_log.warning(
            "pii_gate_blocked count=%d critical=%d",
            len(detections),
            len(critical),
        )
        raise PiiLeakError(critical)


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
