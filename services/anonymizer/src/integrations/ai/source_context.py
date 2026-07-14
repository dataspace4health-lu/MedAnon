"""Source FHIR server resource context for AI agents.

Both the config-generator and chat agents reason better when they know which
FHIR resource types actually exist on the configured source server (and how
many of each). This module fetches that snapshot  resource types + counts
only, never resource *content*  so the agents can suggest rules grounded in
the real dataset instead of generic boilerplate.

PHI safety: only the CapabilityStatement resource-type list and
``_summary=count`` totals are read. No resource bodies are fetched, so no PHI
ever reaches the prompt. The snapshot is cached briefly to avoid re-scanning
the server on every keystroke.
"""

from __future__ import annotations

import logging
import os
import time

_log = logging.getLogger("medanon.ai.source_context")

# (snapshot_text, expiry_epoch)  module-level so it survives across requests
# within a worker process. Keyed implicitly by the single source server URL.
_CACHE: dict[str, tuple[str, float]] = {}

_TTL_SEC = int(os.environ.get("MEDANON_AI_SOURCE_CONTEXT_TTL_SEC", "300"))

# Cap how many resource types we count  a CapabilityStatement can advertise
# 140+ types, but counting each is one HTTP round-trip. We count the clinically
# relevant ones first and stop at this many present types.
_MAX_TYPES_COUNTED = int(os.environ.get("MEDANON_AI_SOURCE_CONTEXT_MAX_TYPES", "40"))

# Resource types worth counting first (the PHI-bearing / clinically meaningful
# ones). Anything advertised by the server but not in this list is still
# counted, just after these.
_PRIORITY_TYPES = (
    "Patient",
    "Practitioner",
    "PractitionerRole",
    "RelatedPerson",
    "Person",
    "Organization",
    "Observation",
    "Condition",
    "Encounter",
    "Procedure",
    "MedicationRequest",
    "MedicationStatement",
    "AllergyIntolerance",
    "Immunization",
    "DiagnosticReport",
    "DocumentReference",
    "CarePlan",
    "Coverage",
    "Claim",
    "ExplanationOfBenefit",
)


def _source_url() -> str:
    return os.environ.get("FHIR_SOURCE_URL", "").strip()


def get_source_resource_context(
    *,
    base_url: str | None = None,
    token: str | None = None,
    use_cache: bool = True,
) -> str:
    """Return a compact, prompt-ready summary of the source server's resources.

    Example output::

        Source FHIR server currently holds these resource types (with counts):
        - Patient: 124
        - Observation: 5,402
        - Condition: 318
        - DocumentReference: 47
        Generate/suggest rules for the types that are actually present.

    Returns an empty string when no source server is configured or it cannot be
    reached  callers treat that as "no extra context" and fall back to generic
    behaviour. Never raises.
    """
    url = (base_url or _source_url()).strip()
    if not url:
        return ""

    now = time.time()
    if use_cache:
        cached = _CACHE.get(url)
        if cached and cached[1] > now:
            return cached[0]

    try:
        from integrations.fhir.reader import (
            get_capability_statement,
            preflight_resource_count,
        )

        tok = token or os.environ.get("FHIR_SOURCE_TOKEN") or None
        types = get_capability_statement(url, token=tok, timeout=10)
    except Exception as exc:  # noqa: BLE001  context is best-effort
        _log.info("source_context_capability_failed url=%s: %s", url, exc)
        return ""

    if not types:
        return ""

    # Order: priority types first (in declared order), then the rest. Cap total.
    present = set(types)
    ordered = [t for t in _PRIORITY_TYPES if t in present]
    ordered += [t for t in sorted(types) if t not in _PRIORITY_TYPES]
    ordered = ordered[:_MAX_TYPES_COUNTED]

    counts: list[tuple[str, int]] = []
    for rtype in ordered:
        try:
            total = preflight_resource_count(
                url, resource_type=rtype, token=tok, timeout=10
            )
        except Exception:  # noqa: BLE001
            total = -1
        # -1 = count failed; 0 = present-but-empty. Only surface types that
        # actually carry data so the agent does not waste rules on empty types.
        if total and total > 0:
            counts.append((rtype, total))

    if not counts:
        # Server reachable but no counted data  still tell the agent which
        # types are *declared* so it does not invent unrelated resources.
        declared = ", ".join(ordered[:20])
        text = (
            "Source FHIR server is reachable but reported no resource counts. "
            f"Declared resource types include: {declared}.\n"
            "Suggest rules for these declared types."
        )
    else:
        lines = "\n".join(f"- {rtype}: {total:,}" for rtype, total in counts)
        text = (
            "Source FHIR server currently holds these resource types "
            "(name: approximate count):\n"
            f"{lines}\n"
            "Focus your generated/suggested rules on the resource types that "
            "are actually present above. Do not invent rules for types the "
            "server does not have."
        )

    if use_cache:
        _CACHE[url] = (text, now + _TTL_SEC)
    return text


def clear_cache() -> None:
    """Drop the cached snapshot (used by tests)."""
    _CACHE.clear()
