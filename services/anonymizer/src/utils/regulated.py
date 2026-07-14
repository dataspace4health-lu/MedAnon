"""Regulated-mode switch — one flag that tightens fail-soft defaults.

EHDS/TEHDAS2 D7.2 assurance expects a deployment posture where "not assessed"
(NA) and "warn" degradations are not acceptable for a clean release. Setting
``MEDANON_REGULATED_MODE=true`` flips those soft defaults into hard requirements
across the stack, for example:

- pseudonymisation refuses to run without ``MEDANON_HASH_KEY`` (no plain-hash
  fallback even if ``MEDANON_HASH_ALLOW_PLAIN`` is set);
- the Trust Gate's terminology/profile validation must be configured and
  hard-fails instead of degrading to SKIPPED/NA (see ``checks/conformance``);
- the output/disclosure barrier cannot be disabled.

The flag is read at call time (never cached at import) so operators and tests
can toggle it without re-importing modules — matching the convention used by
``pipeline.validation._gate_enabled``.
"""

from __future__ import annotations

import os

_TRUTHY = ("1", "true", "yes", "on")


def regulated_mode() -> bool:
    """Return True when ``MEDANON_REGULATED_MODE`` is set to a truthy value."""
    return os.environ.get("MEDANON_REGULATED_MODE", "").strip().lower() in _TRUTHY


def gate_identifier_mode() -> str:
    """Effective score-summary gate identifier mode (``block``/``warn``).

    ``MEDANON_GATE_IDENTIFIER_MODE=warn`` releases output on a HIPAA-coverage gap.
    Regulated mode forbids that soft release: the mode is forced to ``block``
    regardless of the env override.
    """
    if regulated_mode():
        return "block"
    return os.environ.get("MEDANON_GATE_IDENTIFIER_MODE", "block").strip().lower()


_FALSY = ("0", "false", "no")


def output_gate_enabled() -> bool:
    """Whether the unified output-validation barrier is active.

    ON by default.  ``MEDANON_OUTPUT_GATE_ENABLED=false`` disables it (e.g. a
    throughput-only pipeline over externally-trusted input).  Regulated mode
    forbids that: the barrier cannot be switched off.
    """
    if regulated_mode():
        return True
    return (
        os.environ.get("MEDANON_OUTPUT_GATE_ENABLED", "").strip().lower() not in _FALSY
    )


def raw_pii_scan_enabled() -> bool:
    """Whether the raw-resource PII scan runs.

    Both halves of the barrier consult this: ``pipeline.gate.run_pii_gate`` (the
    choke point every ``process_data_batch`` caller passes through) and
    ``pipeline.validation._run_raw_pii_scan`` (the aggregate barrier used by the
    non-FHIR source adapters).  Keeping the policy here means the two cannot
    disagree, which they previously did: only the latter honoured regulated
    mode, so ``MEDANON_REGULATED_MODE=true`` plus the legacy
    ``MEDANON_PII_GATE=false`` released resources containing an SSN.
    """
    if not output_gate_enabled():
        return False
    if regulated_mode():
        return True
    return os.environ.get("MEDANON_PII_GATE", "").strip().lower() not in _FALSY


def ner_gate_mode() -> str:
    """Whether NER content detections block release (``block``) or warn (``warn``).

    The raw scan has three detection sources.  ``regex`` and ``structural`` are
    deterministic: a matched SSN, or a direct-identifier path present with no
    transformation recorded, is a fact.  ``ner`` is a statistical model, and it
    is the *same* Presidio model that just scrubbed the text — so a hit means
    either a genuine miss or a threshold/entity-set mismatch, and it
    false-positives on clinical prose (drug names, eponyms, hospital names read
    as PERSON/LOCATION).

    Default ``warn``: NER hits are counted and audit-logged but do not block.
    Regulated mode forces ``block`` — no soft release.
    """
    if regulated_mode():
        return "block"
    mode = os.environ.get("MEDANON_PII_GATE_NER_MODE", "warn").strip().lower()
    return mode if mode in ("block", "warn") else "warn"
