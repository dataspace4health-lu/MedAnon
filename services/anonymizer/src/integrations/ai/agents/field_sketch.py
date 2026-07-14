"""Compact FHIR field sketch for AI context.

Collapses many sampled FHIR resources per type into a compact, PHI-safe schema
sketch: one line per distinct leaf path with its JSON value type, presence
frequency, array cardinality, and a small value digest. The result feeds the
same ``field_context`` seam that ``config_chat`` and ``field_scanner`` already
consume, so it is a drop-in, richer replacement for the raw ``Type.path : type``
tree the client sends today.

Why a sketch and not the resources: FHIR instances are deeply repetitive, so
sending resources wastes the context window. Collapsing N instances into a
per-type field union is O(distinct paths), not O(resources x fields): 50
Patients become ~50-70 lines regardless of instance count.

PHI safety (the default sketch is PHI-free):
  - Identifying leaves (names, addresses, identifier values, dates, free text)
    get a format SHAPE ("AAAAAAA###", "YYYY-MM-DD"), never a raw value.
  - Only CODE-LIKE leaves (booleans, ``.code`` / ``.system`` / status / use /
    gender ...) emit their literal value DOMAIN, because those values are
    non-identifying by nature and are the signal the model needs (SSN system vs
    MRN system, {male, female}).
  - Raw sample values appear ONLY when ``include_values=True``. Callers pair
    that flag with the provider local-guard (``phi_payload=True``), which is
    fail-closed, so raw values never leave a self-hosted model.

Determinism: output is fully sorted (importance tier, then type, then path), so
identical input yields identical output and the result is safe to cache.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

_log = logging.getLogger("medanon.ai.field_sketch")

# --- shape / sampling caps -------------------------------------------------
_MAX_DEPTH = 6
_DISTINCT_MAX = 12  # distinct values retained per leaf (enough to judge enum)
_ENUM_MAX = 8  # max distinct code-like values rendered as a domain set
_ENUM_VAL_MAX = 40  # cap on a single code-like value (keeps full system URLs)
_SHAPE_MAX = 20  # cap on a format shape string
_SAMPLE_MAX = 24  # cap on a raw sample value (include_values mode)
_STORE_MAX = 64  # cap on a stored value before dedup (bounds memory)
_DEFAULT_CHAR_BUDGET = int(os.environ.get("MEDANON_AI_SKETCH_BUDGET", "16000"))

# JSON keys never worth walking: provenance / parser noise. Mirrors the client
# flattener (fhirFields.ts). ``meta`` is kept (meta.lastUpdated is a date a
# de-id config transforms) but ranked lowest so it drops first under budget.
_SKIP_KEYS = frozenset(
    {"contained", "modifierExtension", "implicitRules", "fhir_comments"}
)

# Terminal path segments whose VALUES are non-identifying codes/enums, so their
# literal domain is safe to show even in the default (PHI-free) sketch. Compared
# case-insensitively. Anything not here is treated as potentially identifying
# and shape-masked instead.
_CODELIKE_SEG = frozenset(
    {
        "code",
        "system",
        "use",
        "status",
        "gender",
        "mode",
        "priority",
        "intent",
        "active",
        "unit",
        "comparator",
        "class",
        "resourcetype",
        "valuecode",
        "valueboolean",
        "primarysource",
        "type",
    }
)

# Substrings marking DIRECT identifiers (Tier 0) and quasi-identifiers (Tier 1).
# Ranking only decides drop order under budget, never PHI safety, so loose
# substring matching is acceptable here.
_DIRECT_ID_MARKERS = (
    "name",
    "identifier.value",
    "telecom",
    "address",
    "birthdate",
    "deceased",
    ".reference",
    ".display",
    "geolocation",
    "latitude",
    "longitude",
    "mothersmaiden",
    "photo",
    "contact.",
    "text.div",
    ".note",
    "annotation",
    "subscriberid",
    "dependent",
    "serialnumber",
    "lotnumber",
    "udicarrier",
    "carrierhrf",
    "birthplace",
    "valuestring",
)
_QUASI_MARKERS = (
    "date",
    "period",
    "time",
    "onset",
    "abatement",
    "issued",
    "authored",
    "recorded",
    "occurrence",
    "effective",
    "gender",
    "maritalstatus",
    "race",
    "ethnicity",
    "language",
)

# Resource-type display priority (clinically / PHI-relevant first). Drives both
# output ordering and which types survive a tight budget first.
_TYPE_PRIORITY = (
    "Patient",
    "Practitioner",
    "PractitionerRole",
    "RelatedPerson",
    "Person",
    "Encounter",
    "Condition",
    "Observation",
    "Procedure",
    "MedicationRequest",
    "MedicationStatement",
    "AllergyIntolerance",
    "Immunization",
    "DiagnosticReport",
    "DocumentReference",
    "CarePlan",
    "Coverage",
    "Organization",
    "Location",
)
_TYPE_RANK = {t: i for i, t in enumerate(_TYPE_PRIORITY)}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_SSN_RE = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


@dataclass
class _Leaf:
    """Accumulated stats for one distinct leaf path across a resource sample."""

    jstype: str = ""
    array: bool = False
    count: int = 0  # number of sampled resources that contain this path
    values: set[str] = field(default_factory=set)
    overflow: bool = False  # more distinct values existed than we retained


def _jstype(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _walk(node: object, prefix: str, local: dict[str, _Leaf], depth: int, in_array: bool) -> None:
    """Populate ``local`` (per-resource) with index-free leaf paths + values."""
    if depth > _MAX_DEPTH or node is None:
        return
    if isinstance(node, (bool, int, float, str)):
        leaf = local.get(prefix)
        if leaf is None:
            leaf = _Leaf(jstype=_jstype(node), array=in_array)
            local[prefix] = leaf
        leaf.array = leaf.array or in_array
        if not leaf.jstype:
            leaf.jstype = _jstype(node)
        if len(leaf.values) < _DISTINCT_MAX:
            leaf.values.add(str(node)[:_STORE_MAX])
        else:
            leaf.overflow = True
        return
    if isinstance(node, list):
        if not node:
            return
        many = len(node) > 1
        for item in node:
            if isinstance(item, (bool, int, float, str)):
                _walk(item, prefix, local, depth, True)
            elif isinstance(item, dict):
                _walk(item, prefix, local, depth, in_array or many)
        return
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SKIP_KEYS:
                continue
            # FHIR primitive-extension siblings ("_birthDate") carry only id /
            # extension metadata, not the value itself.
            if key.startswith("_") and len(key) > 1:
                continue
            child = f"{prefix}.{key}" if prefix else key
            _walk(value, child, local, depth + 1, in_array)


def _accumulate(resources: list[dict]) -> dict[str, _Leaf]:
    """Merge per-resource leaves into a per-type union with presence counts."""
    glob: dict[str, _Leaf] = {}
    for res in resources:
        if not isinstance(res, dict):
            continue
        local: dict[str, _Leaf] = {}
        for key, value in res.items():
            if key in _SKIP_KEYS:
                continue
            if key.startswith("_") and len(key) > 1:
                continue
            _walk(value, key, local, 0, False)
        for path, loc in local.items():
            g = glob.get(path)
            if g is None:
                g = _Leaf(jstype=loc.jstype, array=loc.array)
                glob[path] = g
            g.count += 1
            g.array = g.array or loc.array
            if not g.jstype:
                g.jstype = loc.jstype
            g.overflow = g.overflow or loc.overflow
            for value in loc.values:
                if len(g.values) < _DISTINCT_MAX:
                    g.values.add(value)
                else:
                    g.overflow = True
    return glob


def _is_codelike(path: str, seg: str, jstype: str) -> bool:
    """True when the leaf's VALUE domain is safe to show literally (non-PHI)."""
    if jstype == "boolean":
        return True
    s = seg.lower()
    if s in _CODELIKE_SEG:
        return True
    # FHIR extension URLs are definitional (they name WHICH extension this is:
    # us-core-race, geolocation, patient-mothersMaidenName), never patient data,
    # and are the key signal for targeting extension-based de-id rules.
    return s == "url" and "extension" in path.lower().split(".")


def _short_code_value(value: str) -> str:
    """Render a code-like value compactly, keeping its distinguishing part.

    Short values pass through verbatim. Long URLs/URNs are shortened to their
    trailing segment (``…/us-core-race``) because for a canonical URL the tail,
    not the shared prefix, carries the meaning.
    """
    if len(value) <= _ENUM_VAL_MAX:
        return value
    if "://" in value or value.startswith("urn:"):
        tail = value.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return "…/" + tail[:_ENUM_VAL_MAX]
    return value[:_ENUM_VAL_MAX] + "…"


def _semantic_tag(value: str) -> str:
    if _EMAIL_RE.match(value):
        return "email"
    if _SSN_RE.match(value):
        return "ssn"
    if _URL_RE.match(value):
        return "url"
    if _DATETIME_RE.match(value):
        return "datetime"
    if _DATE_RE.match(value):
        return "date"
    digits = sum(c.isdigit() for c in value)
    if digits >= 7 and all(c.isdigit() or c in " -()+." for c in value):
        return "phone"
    return ""


def _shape(value: str) -> str:
    """Format signature of a value: digits -> #, letters -> A, separators kept.

    Reveals the value's SHAPE (so the model can tell an SSN from an MRN from a
    phone) without revealing the value itself. Dates render canonically.
    """
    value = value.strip()
    tag = _semantic_tag(value)
    if tag == "date":
        return "YYYY-MM-DD"
    if tag == "datetime":
        return "YYYY-MM-DDThh:mm:ss"
    core = value[:_SHAPE_MAX]
    masked = "".join(
        "#" if c.isdigit() else "A" if c.isalpha() else c for c in core
    )
    if len(value) > _SHAPE_MAX:
        masked += "…"
    return f"{masked} [{tag}]" if tag else masked


def _digest(path: str, leaf: _Leaf, include_values: bool) -> str:
    seg = path.rsplit(".", 1)[-1] if "." in path else path
    if _is_codelike(path, seg, leaf.jstype):
        vals = sorted(_short_code_value(v) for v in leaf.values)[:_ENUM_MAX]
        if not vals:
            return ""
        more = leaf.overflow or len(leaf.values) > _ENUM_MAX
        return "{" + ", ".join(vals) + (", …}" if more else "}")
    if not leaf.values:
        return ""
    sample = sorted(leaf.values)[0]
    if include_values:
        clipped = sample[:_SAMPLE_MAX]
        if len(sample) > _SAMPLE_MAX:
            clipped += "…"
        return f'e.g. "{clipped}"'
    return _shape(sample)


def _tier(path: str) -> int:
    """Importance tier: 0 = direct identifier ... 3 = structural noise."""
    lp = path.lower()
    segs = lp.split(".")
    if segs[0] == "meta" or segs[-1] in {
        "versionid",
        "implicitrules",
        "resourcetype",
        "fullurl",
    }:
        return 3
    if any(m in lp for m in _DIRECT_ID_MARKERS):
        return 0
    if any(m in lp for m in _QUASI_MARKERS):
        return 1
    return 2


def _type_rank(rtype: str) -> int:
    return _TYPE_RANK.get(rtype, len(_TYPE_PRIORITY))


def _header(rtype: str, n: int) -> str:
    return f"# {rtype} (n={n})"


def _format_leaf(rtype: str, path: str, leaf: _Leaf, n: int, include_values: bool) -> str:
    full = f"{rtype}.{path}" if path else rtype
    jt = leaf.jstype + ("[]" if leaf.array else "")
    freq = round(100 * leaf.count / n) if n else 0
    digest = _digest(path, leaf, include_values)
    line = f"{full} : {jt}  {freq}%"
    if digest:
        line += f"  {digest}"
    return line


def build_field_sketch(
    resources_by_type: dict[str, list[dict]],
    *,
    include_values: bool = False,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
) -> dict:
    """Build a compact, PHI-safe schema sketch from sampled resources.

    Returns ``{"sketch", "types", "leaf_count", "included_count", "truncated"}``.
    When the sketch exceeds ``char_budget``, the LEAST important leaves (highest
    tier) are dropped first across all types, so direct identifiers always
    survive; each type notes how many of its fields were omitted.
    """
    entries: list[tuple[int, int, str, str, str]] = []
    counts: dict[str, int] = {}
    total_leaves = 0
    for rtype, resources in resources_by_type.items():
        resources = resources or []
        if not resources:
            continue
        n = len(resources)
        counts[rtype] = n
        for path, leaf in _accumulate(resources).items():
            line = _format_leaf(rtype, path, leaf, n, include_values)
            entries.append((_tier(path), _type_rank(rtype), rtype, path, line))
            total_leaves += 1

    # Importance-first: tier, then type priority, then path (deterministic).
    entries.sort(key=lambda e: (e[0], e[1], e[3]))

    included: dict[str, list[tuple[str, str]]] = {}
    dropped: dict[str, int] = {}
    used = 0
    for _tier_v, _trank, rtype, path, line in entries:
        extra = len(line) + 1
        if rtype not in included:
            extra += len(_header(rtype, counts[rtype])) + 1
        if used + extra > char_budget:
            dropped[rtype] = dropped.get(rtype, 0) + 1
            continue
        included.setdefault(rtype, []).append((path, line))
        used += extra

    out: list[str] = []
    for rtype in sorted(included, key=lambda t: (_type_rank(t), t)):
        out.append(_header(rtype, counts[rtype]))
        for _path, line in sorted(included[rtype], key=lambda x: x[0]):
            out.append(line)
        omitted = dropped.get(rtype, 0)
        if omitted:
            out.append(
                f"# … {omitted} lower-priority {rtype} field(s) omitted "
                "(tighten scope for full coverage)"
            )

    included_count = sum(len(v) for v in included.values())
    return {
        "sketch": "\n".join(out),
        "types": sorted(counts, key=lambda t: (_type_rank(t), t)),
        "leaf_count": total_leaves,
        "included_count": included_count,
        "truncated": included_count < total_leaves,
    }


# --- server-side sampling --------------------------------------------------
# PHI-free sketches are cached per (url, types, n); value-bearing sketches are
# never cached (they hold PHI). Mirrors integrations/ai/source_context.py.
_CACHE: dict[tuple, tuple[dict, float]] = {}
_TTL_SEC = int(os.environ.get("MEDANON_AI_SKETCH_TTL_SEC", "300"))


def sample_source_and_sketch(
    resource_types: list[str],
    *,
    n_per_type: int = 25,
    include_values: bool = False,
    base_url: str | None = None,
    token: str | None = None,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
    use_cache: bool = True,
) -> dict:
    """Sample up to ``n_per_type`` resources per type and build a sketch.

    Never raises: returns ``{"sketch": "", "source": "error", "detail": ...}``
    when no source server is configured or nothing could be sampled.
    """
    import time

    url = (base_url or os.environ.get("FHIR_SOURCE_URL", "")).strip()
    if not url:
        return {"sketch": "", "types": [], "source": "error",
                "detail": "no source FHIR server configured (set FHIR_SOURCE_URL)"}
    types = [t for t in dict.fromkeys(resource_types) if t]
    if not types:
        return {"sketch": "", "types": [], "source": "error",
                "detail": "no resource types requested"}

    now = time.time()
    cache_key = (url, tuple(sorted(types)), n_per_type, char_budget)
    if use_cache and not include_values:
        hit = _CACHE.get(cache_key)
        if hit and hit[1] > now:
            return hit[0]

    from integrations.fhir.reader import fetch_resource_type

    tok = token or os.environ.get("FHIR_SOURCE_TOKEN") or None
    by_type: dict[str, list[dict]] = {}
    for rtype in types:
        collected: list[dict] = []
        try:
            for res in fetch_resource_type(
                url, rtype, params={"_count": n_per_type}, token=tok, timeout=15
            ):
                collected.append(res)
                if len(collected) >= n_per_type:
                    break
        except Exception as exc:  # noqa: BLE001 - sampling is best-effort
            _log.info("sketch_sample_failed type=%s: %s", rtype, exc)
        if collected:
            by_type[rtype] = collected

    if not by_type:
        return {"sketch": "", "types": [], "source": "error",
                "detail": "no resources sampled from source server"}

    result = build_field_sketch(
        by_type, include_values=include_values, char_budget=char_budget
    )
    result["source"] = "fhir"
    result["detail"] = ""
    if use_cache and not include_values:
        _CACHE[cache_key] = (result, now + _TTL_SEC)
    return result


def group_by_resource_type(resources: list[dict]) -> dict[str, list[dict]]:
    """Group a flat resource list by ``resourceType`` (skips non-dict/typeless)."""
    by_type: dict[str, list[dict]] = {}
    for res in resources:
        if isinstance(res, dict):
            rtype = res.get("resourceType")
            if isinstance(rtype, str) and rtype:
                by_type.setdefault(rtype, []).append(res)
    return by_type


def clear_cache() -> None:
    """Drop cached sketches (used by tests)."""
    _CACHE.clear()
