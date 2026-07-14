"""Relational conformance (verification): literal references resolve within the
batch. Relative ``ResourceType/id`` refs resolve against the batch Type/id set;
absolute / ``urn:uuid:`` refs resolve against Bundle ``fullUrl`` values (only when
a fullUrl context is supplied, else NA — no false dangling-reference violations).
"""

from __future__ import annotations

from constants import threshold_for
from passport import CheckResult

from checks.conformance._shared import _iter_references


def _reference_integrity(resources, thresholds, full_urls=None):
    """References resolve within the batch.

    Two reference shapes are checked:
      * relative ``ResourceType/id`` → resolves against the batch's Type/id set;
      * absolute / ``urn:uuid:`` (Bundle ``fullUrl``) → resolves against the set
        of entry ``fullUrl`` values. These are only assessed when ``full_urls`` is
        supplied (a Bundle was flattened); for a bare resource list they remain NA
        rather than producing false dangling-reference violations.
    """
    chk = CheckResult(
        check_id="conformance.reference_integrity",
        category="conformance",
        subcategory="relational",
        context="verification",
        threshold=threshold_for("conformance.reference_integrity", thresholds),
        description="Literal references resolve to a resource present in the batch.",
        recommendation="Include referenced resources or fix dangling references.",
        hdqt_category="availability",
        hdqt_dimension="missing",
    )
    present = {
        f"{r.get('resourceType')}/{r.get('id')}"
        for r in resources
        if isinstance(r, dict) and r.get("resourceType") and r.get("id")
    }
    full_set = set(full_urls or [])
    for res in resources:
        if not isinstance(res, dict):
            continue
        for ref in _iter_references(res):
            if ref.startswith("#"):
                continue  # contained-resource reference — not a batch reference
            if ref.startswith(("urn:", "http://", "https://")):
                # Absolute / urn:uuid reference — resolvable only against Bundle
                # fullUrls. NA when no fullUrl context was provided.
                if not full_set:
                    continue
                chk.applicable += 1
                if ref not in full_set:
                    chk.violations += 1
                    chk.add_detail(
                        resource_type=res.get("resourceType"),
                        resource_id=res.get("id", ""),
                        path="reference",
                        detail=f"reference to {ref} not present in the batch",
                    )
                continue
            if "/" not in ref:
                continue
            # Normalize to the exact "ResourceType/id" key (drop query string and
            # any /_history/{vid} suffix) and require an exact match — a loose
            # endswith() can false-resolve "Patient/1" against "RelatedPerson/x1"
            # or match the right id under the wrong resource type.
            short = _normalize_ref(ref)
            if not short:
                continue
            chk.applicable += 1
            if short not in present and short not in full_set:
                chk.violations += 1
                chk.add_detail(
                    resource_type=res.get("resourceType"),
                    resource_id=res.get("id", ""),
                    path="reference",
                    detail=f"reference to {short} not present in the batch",
                )
    return chk


def _normalize_ref(ref: str) -> str:
    """Reduce a literal reference to its exact 'ResourceType/id' key.

    Strips the query string and any '/_history/{vid}' version suffix. Returns ''
    when the reference does not have a recognizable Type/id shape.
    """
    base = ref.split("?", 1)[0]
    hist = base.find("/_history/")
    if hist != -1:
        base = base[:hist]
    parts = base.rstrip("/").rsplit("/", 2)
    if len(parts) >= 2 and parts[-2] and parts[-1]:
        return f"{parts[-2]}/{parts[-1]}"
    return ""
