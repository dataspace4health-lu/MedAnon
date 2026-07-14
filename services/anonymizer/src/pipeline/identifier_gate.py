"""Structural coverage check for HIPAA direct identifiers.

The raw PII scan in :mod:`integrations.ai.agents.pii_detector` is a *content*
scanner: it walks strings of >= 15 characters looking for patterns and NER
entities.  On a FHIR resource the direct identifiers live in short structured
fields  ``name.family`` (5 chars), ``identifier.value`` (8), ``telecom.value``
(12)  so the content scanner never sees them.  A Patient whose every direct
identifier leaked produces zero detections.

Lowering the length threshold does not fix this: NER on a bare surname without
surrounding context is unreliable, and regex on short strings false-positives on
codes and display names.  Content scanning cannot answer "is this string PHI".

It can be answered *structurally*: for the sensitive paths this resource type is
known to carry, is the path present in the output, and did any rule transform
it?  That is deterministic, needs no model, and costs a handful of dict lookups.

``HIPAA_SENSITIVE_PATHS`` (34 resource types) and the two path helpers are
reused verbatim from ``pipeline.scoring.privacy``, which already performs this
computation as ``identifier_risk``  but only when ``MEDANON_SCORING_ENABLED``
is set, and only as a score contribution rather than a gate.  This module
exposes the same judgement as a hard, always-available predicate.

``pipeline/scoring/`` is manually kept in sync with ``services/scoring/src/``
(see ``scripts/sync_shared_code.sh``), so nothing here modifies it; the helpers
are imported, not moved.
"""

from __future__ import annotations

import logging

from pipeline.manifest import _MANIFEST_ENABLED
from pipeline.scoring.constants import HIPAA_SENSITIVE_PATHS
from pipeline.scoring.privacy import _is_bare_reference, _nested_path_exists

_log = logging.getLogger("medanon.identifier_gate")

# Actions that transform a field only when they find something.  A clean field
# produces no manifest entry even though the rule ran and the field is safe, so
# a path targeted by one of these is treated as covered by configuration.
_CONDITIONAL_ACTIONS = frozenset({"nlp_detect_act", "nlp_scrub", "nlp_detect"})


# ``HIPAA_SENSITIVE_PATHS`` is a flat path list with no severity, but its 70-odd
# leaf names fall into three risk classes.  We grade them the same way
# ``pii_detector._SEVERITY_MAP`` grades content detections, so that one env var
# (``MEDANON_PII_GATE_BLOCK_SEVERITY``) governs both halves of the gate.
#
# ``critical`` / ``high`` are HIPAA Safe Harbor *direct* identifiers: an
# untransformed one in released output is a reportable leak.
#
# Everything else is ``medium``: temporal quasi-identifiers (``period``,
# ``effectiveDateTime``, ``onsetDateTime``, …) and provenance references
# (``subject``, ``performer``, ``recorder``, …).  Dates are deliberately not
# blocking  a date-shift pipeline legitimately retains day precision, which is
# the same reason ``pii_detector`` scores ``date_iso`` as ``medium``.  Bare
# References are already treated as covered (see ``uncovered_sensitive_paths``);
# a Reference carrying a ``display`` name surfaces here as a ``medium`` warning.
_CRITICAL_LEAVES = frozenset(
    {
        "name",
        "identifier",
        "photo",
        "link",
        "serialNumber",
        "distinctIdentifier",
        "subscriberId",
        "udiCarrier",
        "lotNumber",
    }
)
_HIGH_LEAVES = frozenset({"telecom", "address", "position"})


def path_severity(sensitive_path: str) -> str:
    """Grade a ``HIPAA_SENSITIVE_PATHS`` entry as critical / high / medium."""
    leaf = sensitive_path.split(".")[-1]
    if leaf in _CRITICAL_LEAVES:
        return "critical"
    if leaf in _HIGH_LEAVES:
        return "high"
    return "medium"


def manifest_recording_enabled() -> bool:
    """Whether rule execution is being recorded into the transformation manifest.

    The structural check reads the manifest to learn which paths were
    transformed.  With recording off, every present sensitive path would look
    uncovered and the gate would block everything  so the check is skipped
    instead.  ``docker-compose.yml`` sets ``MEDANON_MANIFEST_ENABLED=true``;
    the code default is off.
    """
    return bool(_MANIFEST_ENABLED)


def _covered_paths(manifest_entries: list[dict]) -> set[str]:
    """Paths the manifest says were transformed, both full and type-stripped."""
    covered: set[str] = set()
    for entry in manifest_entries:
        path = entry.get("path", "")
        if not path:
            continue
        covered.add(path)
        parts = path.split(".", 1)
        if len(parts) == 2:
            covered.add(parts[1])  # "Patient.name" -> "name"
    return covered


def _config_covered_paths(settings) -> set[str]:
    """Leaf paths targeted by a conditional (find-then-transform) rule."""
    covered: set[str] = set()
    rules = getattr(settings, "rules", None) or []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("action") not in _CONDITIONAL_ACTIONS:
            continue
        match_expr = rule.get("match", "")
        if not isinstance(match_expr, str) or not match_expr:
            continue
        if match_expr.startswith("*."):
            covered.add(match_expr[2:])
        elif "." in match_expr:
            covered.add(match_expr.split(".", 1)[1])
        else:
            covered.add(match_expr)
    return covered


def uncovered_sensitive_paths(
    resource: dict,
    manifest_entries: list[dict],
    settings=None,
) -> list[str]:
    """Sensitive paths *present* in *resource* that no rule transformed.

    Mirrors ``scoring.privacy.PrivacyEvaluator._identifier_detection``, but
    returns the offending paths rather than a risk fraction.

    Absent paths are excluded: they cannot leak.  ``id`` and bare FHIR
    References are treated as covered  an opaque server key is not, on its own,
    re-identifying, and reference rewriting handles it transitively.
    """
    if not isinstance(resource, dict):
        return []
    rtype = resource.get("resourceType", "")
    if not rtype:
        return []

    sensitive = list(HIPAA_SENSITIVE_PATHS.get(rtype, []))
    sensitive.extend(HIPAA_SENSITIVE_PATHS.get("*", []))
    if not sensitive:
        return []  # not a PHI-bearing resource type

    covered_paths = _covered_paths(manifest_entries or [])
    config_covered = _config_covered_paths(settings) if settings is not None else set()

    uncovered: list[str] = []
    for s_path in sensitive:
        covered = any(
            s_path == cp or cp.startswith(s_path + ".") or s_path.startswith(cp + ".")
            for cp in covered_paths
        )
        leaf = s_path.split(".")[-1]
        if s_path in config_covered or leaf in config_covered:
            covered = True

        if "." in s_path:
            present = _nested_path_exists(resource, s_path)
        else:
            present = s_path in resource
            if present and (s_path == "id" or _is_bare_reference(resource[s_path])):
                covered = True

        if present and not covered:
            uncovered.append(s_path)

    return uncovered


def structural_detections(
    results: list[dict],
    manifest_entries_list: list[list[dict]] | None,
    settings=None,
) -> list[dict]:
    """Graded detections for every present-but-untransformed sensitive path.

    Returns an empty list (and logs) when the manifest is unavailable  without
    it the check cannot distinguish "no rule fired" from "nothing was recorded",
    and blocking on that would reject every batch.

    Severity comes from :func:`path_severity`; the caller decides which
    severities block (``pii_detector.block_severities``, i.e. the same
    ``MEDANON_PII_GATE_BLOCK_SEVERITY`` that governs content detections).

    Detection dicts deliberately carry only ``resource_type`` and the path: they
    travel into audit logs and error payloads, so no identifier value and no
    resource id go with them.  That also makes per-resource duplicates literally
    identical, so results are collapsed to one entry per
    ``(resource_type, field_path)`` carrying an ``affected_resources`` count
    a 1000-resource batch with an uncovered ``Patient.name`` yields one
    detection, not a thousand.
    """
    if manifest_entries_list is None or not manifest_recording_enabled():
        _log.debug(
            "structural_gate_skipped  manifest recording disabled "
            "(set MEDANON_MANIFEST_ENABLED=true to enable the structural check)"
        )
        return []

    counts: dict[tuple[str, str], int] = {}
    for resource, entries in zip(results, manifest_entries_list):
        rtype = resource.get("resourceType", "unknown")
        for path in uncovered_sensitive_paths(resource, entries, settings):
            counts[(rtype, path)] = counts.get((rtype, path), 0) + 1

    return [
        {
            "resource_type": rtype,
            "field_path": path,
            "type": "uncovered_sensitive_path",
            "evidence": f"present, no transformation recorded: {path}",
            "confidence": 1.0,
            "severity": path_severity(path),
            "source": "structural",
            "affected_resources": n,
        }
        for (rtype, path), n in sorted(counts.items())
    ]
