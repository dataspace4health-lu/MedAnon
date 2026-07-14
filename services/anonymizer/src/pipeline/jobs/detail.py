"""Server-side job-detail computation (resource counts + field/PII summary).

This is the data the Jobs UI renders for a completed export. It used to be built
in the browser: `JobDetailPanel` downloaded the whole NDJSON result into a Blob
and walked it. Two problems with that:

1. **Volume.** A 200 MB export is not a slow render, it is a dead tab. And since
   the detail cache was only written *after* a successful client-side parse, a
   job too large to parse could never populate it — so it re-downloaded and
   re-failed on every open, permanently.
2. **Correctness.** The PII map is derived *solely* from each resource's
   transformation manifest (``meta.tag``). Released NDJSON no longer carries that
   tag — :func:`pipeline.manifest.strip_manifest_tag` removes it so the manifest
   ships as a separate artifact — so a browser-side parse of the released data now
   yields an empty PII map. The worker is the only place the manifest and the
   resource are both in hand.

So the worker accumulates the detail during the single pass it already makes over
every resource (:meth:`JobSummaryCollector.record_resource`), and writes it to the
job-detail store at finalize. The browser reads it and never touches the NDJSON.

The field-walk and aggregation mirror ``client/src/lib/fhirFields.ts`` and
``client/src/lib/piiDetection.ts`` so the rendered shape is unchanged.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict

_log = logging.getLogger("medanon.job_detail")

# Mirrors client/src/lib/fhirFields.ts
_SKIP_KEYS = frozenset({"meta", "contained", "modifierExtension", "implicitRules"})
_SKIP_NESTED = frozenset({"modifierExtension", "contained", "fhir_comments"})
_MAX_DEPTH = 5

# Mirrors bulkHelpers.tsx: the deep field-walk is capped per resource type because
# the schema stabilises quickly; counts are scaled back up to the true total.
_FIELD_SAMPLE_LIMIT = 200


def _walk_value(paths: list[str], key: str, value, depth: int) -> None:
    if depth > _MAX_DEPTH or value is None:
        return

    if isinstance(value, (str, int, float, bool)):
        paths.append(key)
        return

    if isinstance(value, list):
        if not value:
            return
        first = value[0]
        if isinstance(first, (str, int, float, bool)):
            # Scalar array (e.g. name.given) collapses to the one path.
            paths.append(key)
            return
        # Object array: walk EVERY element so heterogeneous siblings surface
        # their leaves. Paths stay index-free so they match the manifest.
        for item in value:
            if isinstance(item, dict):
                _walk_value(paths, key, item, depth)
        return

    if isinstance(value, dict):
        for k, v in value.items():
            if k in _SKIP_NESTED:
                continue
            _walk_value(paths, f"{key}.{k}", v, depth + 1)


def extract_field_paths(resource: dict) -> list[str]:
    """Deep-extract dotted field paths ("name.family", "code.coding.display").

    Array indices are never emitted, so paths line up with manifest paths. The
    first occurrence of each path wins, preserving order.
    """
    paths: list[str] = []
    for key, value in resource.items():
        if key in _SKIP_KEYS or value is None:
            continue
        _walk_value(paths, key, value, 0)

    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def simplify_path(full_path: str) -> str:
    """Strip the resourceType prefix and array indices from a manifest FHIRPath.

    ``Patient.name[0].family`` -> ``name.family``
    """
    dot = full_path.find(".")
    rest = full_path[dot + 1 :] if dot >= 0 else full_path
    out: list[str] = []
    skipping = False
    for ch in rest:
        if ch == "[":
            skipping = True
        elif ch == "]":
            skipping = False
        elif not skipping:
            out.append(ch)
    return "".join(out)


def parent_key(path: str) -> str:
    """Top-level key of a dotted path: ``name.family`` -> ``name``."""
    dot = path.find(".")
    return path[:dot] if dot >= 0 else path


class JobDetailAccumulator:
    """Accumulates the job-detail payload in one pass over the resources.

    PII entries are aggregated across *every* resource (the worker sees them all
    and holds the manifest), so their counts are exact rather than sampled. The
    deep field-walk is sampled per type and scaled, matching the client.
    """

    def __init__(self) -> None:
        self._type_counts: Counter[str] = Counter()
        self._error_count = 0
        # resourceType -> "field::action" -> count
        self._pii: dict[str, Counter[str]] = defaultdict(Counter)
        # resourceType -> fieldPath -> count
        self._fields: dict[str, Counter[str]] = defaultdict(Counter)
        self._field_sampled: Counter[str] = Counter()

    def record(self, resource: dict, manifest_entries: list[dict] | None) -> None:
        if not isinstance(resource, dict):
            return
        rtype = resource.get("resourceType", "Unknown")
        if "error" in resource:
            self._error_count += 1
            return
        self._type_counts[rtype] += 1

        # PII map: manifest is the sole authoritative record of what was
        # transformed. Never infer from output values — that mislabels untouched
        # fields (a 64-hex coding.display read as "cryptohash").
        if manifest_entries:
            seen: set[str] = set()
            for entry in manifest_entries:
                if not isinstance(entry, dict):
                    continue
                action = entry.get("action")
                path = entry.get("path")
                if not action or not path:
                    continue
                key = f"{simplify_path(str(path))}::{action}"
                if key not in seen:
                    seen.add(key)
                    self._pii[rtype][key] += 1

        # Deep field-walk, capped per type.
        self._field_sampled[rtype] += 1
        if self._field_sampled[rtype] <= _FIELD_SAMPLE_LIMIT:
            counts = self._fields[rtype]
            for path in extract_field_paths(resource):
                counts[path] += 1

    def _pii_data(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for rtype, fields in self._pii.items():
            entries = []
            for key, count in fields.items():
                field_path, _, action = key.partition("::")
                entries.append(
                    {"fieldPath": field_path, "action": action, "count": count}
                )
            entries.sort(key=lambda e: (-e["count"], e["fieldPath"]))
            out[rtype] = entries
        return out

    def _field_summary(self, pii_data: dict[str, list[dict]]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for rtype, field_counts in self._fields.items():
            if not field_counts:
                continue
            actions: dict[str, str] = {}
            for e in pii_data.get(rtype, []):
                actions.setdefault(e["fieldPath"], e["action"])

            # Scale sampled counts back up to the true per-type total. The
            # sampled total is the most common field count (present on every
            # sampled resource).
            real_total = self._type_counts.get(rtype, 0)
            sampled_total = max(max(field_counts.values()), 1)
            scale = (
                real_total / sampled_total
                if real_total and real_total > sampled_total
                else 1
            )

            entries = []
            for field_path, count in field_counts.items():
                action = actions.get(field_path) or actions.get(parent_key(field_path))
                item: dict = {
                    "fieldPath": field_path,
                    "count": round(count * scale) if scale > 1 else count,
                }
                if action:
                    item["action"] = action
                entries.append(item)
            entries.sort(key=lambda e: (-e["count"], e["fieldPath"]))
            out[rtype] = entries
        return out

    def to_dict(self) -> dict:
        """Render the payload served by ``GET /v1/jobs/{id}/detail``."""
        pii_data = self._pii_data()
        return {
            "resource_counts": dict(self._type_counts),
            "total_resources": sum(self._type_counts.values()) + self._error_count,
            "pii_data": pii_data,
            "field_summary": self._field_summary(pii_data),
        }


def save_job_detail(job_id: str, detail: dict) -> None:
    """Best-effort write of the job detail. Never fails the job."""
    try:
        from pipeline.job_detail import get_job_detail_store

        store = get_job_detail_store()
        if store is None:
            _log.debug("job_detail_store_unset job=%s", job_id)
            return
        store.set(job_id, detail)
        _log.info(
            "job_detail_saved job=%s resources=%d types=%d",
            job_id,
            detail.get("total_resources", 0),
            len(detail.get("resource_counts", {})),
        )
    except Exception:
        _log.debug("job_detail_save_failed job=%s", job_id, exc_info=True)
