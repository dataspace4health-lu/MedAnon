"""Deterministic identifier classification for FHIR field paths.

Labels a leaf path as ``direct`` (HIPAA Safe Harbor direct identifier),
``quasi`` (k-anonymity quasi-identifier), or ``non`` (not identifying). This is
the AUTHORITATIVE source the Resource Explorer consumes, so its per-field labels
match what the engine actually enforces: it reuses ``HIPAA_SENSITIVE_PATHS`` and
the severity grading from :mod:`pipeline.identifier_gate` — the same catalog the
structural output gate blocks on — and adds the standard demographic
quasi-identifiers that Safe Harbor's *direct*-only list omits.

Grounding
---------
- **Direct**: HIPAA Safe Harbor direct identifiers (``HIPAA_SENSITIVE_PATHS``
  graded ``critical``/``high`` by ``identifier_gate.path_severity``): names,
  identifiers, telecom, street address line, photo, precise geolocation, record
  links, plus the resource ``id``.
- **Quasi**: the k-anonymity quasi-identifier set (Sweeney 2002 — date of birth,
  sex, ZIP re-identify ~87% of the US population — plus race/ethnicity, marital
  status, language) and the temporal ``medium`` HIPAA paths (dates/periods) and
  provenance links.
- **Non**: clinical codes, coding systems, structural qualifiers, narrative.

Deterministic, no model, a handful of dict lookups — safe to call per field.
``pipeline/scoring/`` is imported read-only (kept in sync with
``services/scoring/src/`` by ``scripts/sync_shared_code.sh``); nothing here
modifies it.
"""

from __future__ import annotations

import re

from pipeline.identifier_gate import path_severity
from pipeline.scoring.constants import HIPAA_SENSITIVE_PATHS

# "direct" | "quasi" | "non"
IdentifierClass = str

# Structural qualifiers describe an element; never identifiers on their own
# (``telecom.system='phone'``, ``name.use='official'``, ``identifier.system``).
_QUALIFIER_LEAVES = frozenset({"system", "use", "url", "version"})

# Demographic quasi-identifiers NOT in Safe Harbor's direct list. Matched on the
# element leaf name, or a keyword anywhere in the path/URL — race and ethnicity
# live in URL-keyed extensions (``extension.where(url='…us-core-race')``), so the
# keyword must be sought in the full, un-stripped path.
_DEMOGRAPHIC_QUASI_LEAVES = frozenset(
    {"gender", "birthsex", "sex"}
)
_DEMOGRAPHIC_QUASI_KEYWORDS = (
    "race",
    "ethnic",
    "religion",
    "language",
    "maritalstatus",
    "multiplebirth",
)

# Geographic sub-units of an address. HIPAA grades the whole ``address`` element
# as a direct identifier, but ZIP/city/district are generalised in practice
# (Safe Harbor keeps the first three ZIP digits), so per leaf they are quasi.
# State and country are permitted at or above state level.
_GEO_QUASI_LEAVES = frozenset({"city", "district", "postalcode"})
_GEO_PERMITTED_LEAVES = frozenset({"state", "country"})

_INDEX_RE = re.compile(r"\[\d+\]")
_WHERE_RE = re.compile(r"\.where\([^)]*\)")


def _strip(path: str, resource_type: str) -> str:
    """Path minus the resource-type prefix, array indices, and ``.where()``."""
    p = _WHERE_RE.sub("", _INDEX_RE.sub("", path))
    prefix = f"{resource_type}."
    if p.startswith(prefix):
        p = p[len(prefix) :]
    return p


def classify_path(resource_type: str, fhir_path: str) -> IdentifierClass:
    """Classify one FHIR leaf path as ``direct`` / ``quasi`` / ``non``."""
    rel = _strip(fhir_path, resource_type)
    if not rel:
        return "non"
    low = rel.lower()
    leaf = low.rsplit(".", 1)[-1]
    full_low = fhir_path.lower()  # keeps where(url=…) for keyword matching

    if leaf in _QUALIFIER_LEAVES:
        return "non"

    # HIPAA Safe Harbor catalog — the same source the structural gate enforces.
    sensitive = list(HIPAA_SENSITIVE_PATHS.get(resource_type, ()))
    sensitive += list(HIPAA_SENSITIVE_PATHS.get("*", ()))
    for sp in sensitive:
        spl = sp.lower()
        if low != spl and not low.startswith(spl + "."):
            continue
        # Narrative text is free-text content (the value scan's job), not an
        # identifier class of its own.
        if spl == "text":
            return "non"
        # Address: refine per sub-leaf — street line stays direct, ZIP/city
        # generalise as quasi, state/country are permitted.
        if spl.endswith("address"):
            if leaf in _GEO_QUASI_LEAVES:
                return "quasi"
            if leaf in _GEO_PERMITTED_LEAVES:
                return "non"
            return "direct"
        if path_severity(sp) in ("critical", "high"):
            return "direct"
        # medium HIPAA paths: the resource id is a unique record identifier;
        # dates/periods are temporal quasi-identifiers; the rest are provenance
        # links (indirect) — all quasi except the id.
        if leaf == "id":
            return "direct"
        return "quasi"

    # Demographic quasi-identifiers outside Safe Harbor's direct list.
    if leaf in _DEMOGRAPHIC_QUASI_LEAVES:
        return "quasi"
    if any(k in full_low for k in _DEMOGRAPHIC_QUASI_KEYWORDS):
        return "quasi"

    return "non"


def classify_paths(resource_type: str, paths: list[str]) -> dict[str, IdentifierClass]:
    """Classify many paths at once; returns ``{path: class}``."""
    return {p: classify_path(resource_type, p) for p in paths}
