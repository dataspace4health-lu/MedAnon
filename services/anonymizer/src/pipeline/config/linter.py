"""Config profile coverage linter (E1.5).

Statically checks a config profile against a catalogue of well-known PHI/PII
FHIR paths (``KNOWN_PHI_PATHS``) and reports which ones are *not* covered by any
rule's ``match`` expression.  This surfaces gaps — e.g. a profile that redacts
``Patient.name`` but forgets ``Patient.telecom`` — before the profile is ever
run against real data.

The linter is intentionally conservative: it only reasons about static
``match`` strings (simple dot-paths, wildcards, and the leaf segment), never
about runtime data.  A path counts as "covered" when a rule matches it exactly,
matches a parent path it is nested under, or matches the same leaf via a
wildcard.  ``nlp_*`` actions on free-text containers (e.g. ``*.text.div``) are
treated as covering narrative paths since they scrub embedded identifiers.
"""

from __future__ import annotations

# Catalogue of high-risk identifier paths per FHIR resource type.  Keyed by
# resource type; ``*`` entries apply to every resource.  This is a curated,
# non-exhaustive baseline aligned with HIPAA Safe Harbor identifier categories.
KNOWN_PHI_PATHS: dict[str, list[str]] = {
    "*": [
        "text.div",
        "id",
    ],
    "Patient": [
        "name",
        "telecom",
        "address",
        "birthDate",
        "identifier",
        "contact",
        "communication",
        "photo",
        "deceasedDateTime",
    ],
    "Practitioner": [
        "name",
        "telecom",
        "address",
        "identifier",
        "birthDate",
    ],
    "RelatedPerson": [
        "name",
        "telecom",
        "address",
        "identifier",
        "birthDate",
    ],
    "Observation": [
        "valueString",
        "note",
        "subject",
    ],
    "Condition": [
        "note",
        "subject",
    ],
    "Encounter": [
        "subject",
        "participant",
    ],
    "DocumentReference": [
        "content",
        "subject",
        "description",
    ],
}

# Free-text container leaves: any nlp_* / scrub_text rule matching these is
# treated as covering narrative PHI for the resource.
_NARRATIVE_LEAVES = frozenset({"div", "note", "text", "valueString", "description"})
_NLP_ACTIONS = frozenset({"nlp_scrub", "nlp_detect", "nlp_detect_act", "scrub_text"})


def _normalise_match(match: str) -> tuple[str, str]:
    """Return ``(resource_type, leaf_path)`` for a match expression.

    ``"Patient.telecom.value"`` -> ``("Patient", "telecom.value")``
    ``"*.text.div"``            -> ``("*", "text.div")``
    Strips ``.where(...)`` / function tails to the bare dot-path prefix.
    """
    expr = match.strip()
    # Drop everything from the first '(' (function call) onward.
    paren = expr.find("(")
    if paren != -1:
        # Keep the dot-path leading up to the function, trimming a trailing
        # ".where" / ".first" token.
        head = expr[:paren]
        expr = head.rsplit(".", 1)[0] if "." in head else head
    parts = expr.split(".")
    if not parts:
        return ("*", "")
    rtype = parts[0]
    leaf = ".".join(parts[1:]) if len(parts) > 1 else ""
    return (rtype, leaf)


def _is_covered(rtype: str, phi_path: str, rules: list[dict]) -> bool:
    """True if any rule's ``match`` covers ``rtype.phi_path``."""
    phi_leaf = phi_path.split(".")[-1]
    for rule in rules:
        match = rule.get("match")
        if not isinstance(match, str) or not match.strip():
            continue
        m_rtype, m_leaf = _normalise_match(match)

        # Resource type must align (wildcard matches anything).
        if m_rtype not in ("*", rtype):
            continue

        action = rule.get("action", "")

        # Exact or prefix coverage: the rule path equals the PHI path or is an
        # ancestor of it (rule "telecom" covers "telecom.value").
        if m_leaf and (
            m_leaf == phi_path
            or phi_path.startswith(m_leaf + ".")
            or m_leaf.startswith(phi_path + ".")
        ):
            return True

        # Wildcard / leaf coverage for the same final segment.
        if m_leaf and m_leaf.split(".")[-1] == phi_leaf:
            return True

        # NLP/scrub rules over a narrative container cover narrative PHI paths.
        if action in _NLP_ACTIONS and phi_leaf in _NARRATIVE_LEAVES:
            return True
    return False


def lint_profile(parsed_config: dict) -> dict:
    """Lint a parsed config dict for PHI-path coverage gaps.

    Args:
        parsed_config: A profile parsed from YAML (``{"rules": [...], ...}``).

    Returns:
        A report dict::

            {
              "total_known_paths": int,
              "covered": int,
              "uncovered": [{"resource_type": str, "path": str}, ...],
              "coverage_ratio": float,   # covered / total, 1.0 when total == 0
            }
    """
    rules = parsed_config.get("rules") if isinstance(parsed_config, dict) else None
    if not isinstance(rules, list):
        rules = []

    # Which resource types does this profile actually touch?  We lint the union
    # of (a) types referenced by rules and (b) the always-relevant "*" set, so a
    # profile that never mentions Encounter is not flagged for Encounter gaps.
    referenced_types: set[str] = set()
    for rule in rules:
        match = rule.get("match") if isinstance(rule, dict) else None
        if isinstance(match, str) and match.strip():
            rtype, _ = _normalise_match(match)
            if rtype != "*":
                referenced_types.add(rtype)

    uncovered: list[dict] = []
    total = 0
    for rtype, paths in KNOWN_PHI_PATHS.items():
        if rtype != "*" and rtype not in referenced_types:
            continue
        for phi_path in paths:
            total += 1
            if not _is_covered(rtype, phi_path, rules):
                uncovered.append({"resource_type": rtype, "path": phi_path})

    covered = total - len(uncovered)
    return {
        "total_known_paths": total,
        "covered": covered,
        "uncovered": uncovered,
        "coverage_ratio": (covered / total) if total else 1.0,
    }
