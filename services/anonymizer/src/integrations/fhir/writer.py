"""Write-side FHIR operations — single resource, bundle, and batch upload.

Handles resource creation/update, manifest tag stripping, ID sanitisation,
topological upload ordering, and cross-resource reference rewriting.
"""

import os

from ._transport import (
    _RESOURCE_TYPE_RE,
    _sanitise_resource_id,
    _validate_resource_id,
    _validate_resource_type,
    _write_json,
    log,
)

__all__ = [
    "_MANIFEST_SYSTEM",
    "_UPLOAD_BATCH_SIZE",
    "_strip_manifest_tags",
    "post_resource",
    "post_bundle",
    "_infer_upload_tiers",
    "_compute_id_map",
    "_rewrite_references",
    "_build_batch_entry",
    "_parse_batch_response",
    "_post_bundle_batch",
    "upload_resources",
]


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

_MANIFEST_SYSTEM = "https://medanon.local/transformation-manifest"


def _strip_manifest_tags(resource: dict) -> dict:
    """Return a shallow copy of *resource* with transformation-manifest tags removed.

    The MedAnon pipeline attaches ``meta.tag`` entries (system
    ``https://medanon.local/transformation-manifest``) whose ``display``
    field can exceed HAPI FHIR's ``hfj_tag_def.tag_display`` varchar(200)
    column.  These tags are intended for the UI only — strip them before
    uploading to any FHIR server.
    """
    meta = resource.get("meta")
    if not meta or not isinstance(meta, dict):
        return resource
    tags = meta.get("tag")
    if not tags or not isinstance(tags, list):
        return resource

    filtered = [
        t
        for t in tags
        if not (isinstance(t, dict) and t.get("system") == _MANIFEST_SYSTEM)
    ]
    if len(filtered) == len(tags):
        return resource  # nothing removed — avoid copy

    resource = {**resource, "meta": {**meta, "tag": filtered}}
    return resource


def post_resource(base_url, resource, token=None, timeout=30):
    """Create or update a single FHIR resource on a FHIR server.

    - If the resource has an ``id`` field, issues a PUT to preserve that ID.
    - Otherwise issues a POST and lets the server assign an ID.

    Returns the server's response resource dict.
    Raises ``ValueError`` on network or HTTP errors.
    """
    resource = _strip_manifest_tags(resource)

    rt = resource.get("resourceType")
    if not rt:
        raise ValueError("resource is missing resourceType")

    rid = resource.get("id")
    if rid:
        _validate_resource_type(rt)
        _validate_resource_id(rid)
        url = f"{base_url.rstrip('/')}/{rt}/{rid}"
        method = "PUT"
        log.info("PUT %s (resource)", rt)
    else:
        _validate_resource_type(rt)
        url = f"{base_url.rstrip('/')}/{rt}"
        method = "POST"
        log.info("POST %s (no id)", rt)

    return _write_json(
        url, resource, method, token=token, timeout=timeout, operation="write"
    )


def post_bundle(base_url, bundle, token=None, timeout=30):
    """Submit a FHIR Bundle (transaction or batch) to the server's base URL.

    Returns the response Bundle dict.
    Raises ``ValueError`` on network or HTTP errors.
    """
    if bundle.get("resourceType") != "Bundle":
        raise ValueError(
            f"post_bundle expects a FHIR Bundle, got resourceType={bundle.get('resourceType')!r}"
        )
    url = base_url.rstrip("/") + "/"
    log.info("POST Bundle type=%s to %s", bundle.get("type"), url)
    return _write_json(
        url, bundle, "POST", token=token, timeout=timeout, operation="bundle"
    )


_UPLOAD_BATCH_SIZE = int(
    os.environ.get("MEDANON_UPLOAD_BATCH_SIZE", "500")
)  # resources per FHIR batch Bundle


def _infer_upload_tiers(resources: list[dict]) -> dict[str, int]:
    """Return a tier map ``{resourceType: N}`` derived from the reference graph.

    Computes the topological depth of each resource type so that types with no
    dependencies (tier 0) are uploaded before types that reference them (tier 1),
    which are uploaded before types that reference *those* (tier 2), and so on.

    Uses a fast shallow scan: only inspects ``reference`` values in the top
    three dict/list levels of each resource, which covers virtually all FHIR
    reference fields (``subject.reference``, ``entry[].resource.reference``,
    etc.) without an expensive full-depth recursive walk.
    """
    present: set[str] = set()
    for r in resources:
        rt = r.get("resourceType")
        if rt:
            present.add(rt)

    # deps[A] = set of types that A depends on (references) that are also present
    deps: dict[str, set[str]] = {rt: set() for rt in present}

    def _extract_ref_type(val) -> str | None:
        if isinstance(val, str) and "/" in val:
            ref_type = val.split("/", 1)[0]
            return ref_type if ref_type in present else None
        return None

    for r in resources:
        rt = r.get("resourceType")
        if not rt or rt not in present:
            continue
        my_deps = deps[rt]
        # Scan top-level fields
        for v in r.values():
            if isinstance(v, dict):
                ref = v.get("reference")
                if ref:
                    ref_t = _extract_ref_type(ref)
                    if ref_t and ref_t != rt:
                        my_deps.add(ref_t)
                # One level deeper (covers nested dicts like subject.reference)
                for vv in v.values():
                    if isinstance(vv, dict):
                        ref = vv.get("reference")
                        if ref:
                            ref_t = _extract_ref_type(ref)
                            if ref_t and ref_t != rt:
                                my_deps.add(ref_t)
                    elif isinstance(vv, list):
                        for item in vv:
                            if isinstance(item, dict):
                                ref = item.get("reference")
                                if ref:
                                    ref_t = _extract_ref_type(ref)
                                    if ref_t and ref_t != rt:
                                        my_deps.add(ref_t)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        ref = item.get("reference")
                        if ref:
                            ref_t = _extract_ref_type(ref)
                            if ref_t and ref_t != rt:
                                my_deps.add(ref_t)
                        for vv in item.values():
                            if isinstance(vv, dict):
                                ref = vv.get("reference")
                                if ref:
                                    ref_t = _extract_ref_type(ref)
                                    if ref_t and ref_t != rt:
                                        my_deps.add(ref_t)

    # Iterative relaxation: tier[A] = 1 + max(tier[B] for B in deps[A])
    # Converges in at most len(present) passes for a DAG.
    tiers: dict[str, int] = {rt: 0 for rt in present}
    for _ in range(len(present)):
        updated = False
        for rt, dep_types in deps.items():
            if dep_types:
                required = max(tiers[d] for d in dep_types) + 1
                if required > tiers[rt]:
                    tiers[rt] = required
                    updated = True
        if not updated:
            break

    return tiers


def _compute_id_map(resources: list[dict]) -> dict[tuple[str, str], str]:
    """Build ``{(resourceType, originalId) -> sanitisedId}`` for resources whose ID changes.

    Covers ID sanitisation applied by ``_build_batch_entry``:
      - Invalid FHIR ID chars (e.g. underscores) are replaced with hyphens

    Only entries where the sanitised ID differs from the original are included,
    so the map is typically small (only the IDs that were actually changed).
    """
    id_map: dict[tuple[str, str], str] = {}
    for r in resources:
        rt = r.get("resourceType")
        original_id = r.get("id")
        if not rt or not original_id:
            continue
        rid = _sanitise_resource_id(original_id)
        if rid != original_id:
            id_map[(rt, original_id)] = rid
    return id_map


def _rewrite_references(obj: object, id_map: dict[tuple[str, str], str]) -> object:
    """Recursively rewrite FHIR ``reference`` strings using *id_map*.

    Walks *obj* (dict, list, or scalar) and replaces every
    ``{"reference": "ResourceType/originalId"}`` value where the
    (resourceType, originalId) pair exists in *id_map*, producing
    ``"ResourceType/sanitisedId"``.  Only plain ``Type/id`` references
    are rewritten; versioned references (``Type/id/_history/N``) are
    left unchanged.

    Returns a new object — the input is never mutated.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "reference" and isinstance(v, str):
                parts = v.split("/", 1)
                if len(parts) == 2 and "/" not in parts[1]:
                    # Plain Type/id reference — rewrite if in map
                    new_rid = id_map.get((parts[0], parts[1]))
                    out[k] = f"{parts[0]}/{new_rid}" if new_rid else v
                else:
                    out[k] = v
            else:
                out[k] = _rewrite_references(v, id_map)
        return out
    if isinstance(obj, list):
        return [_rewrite_references(item, id_map) for item in obj]
    return obj


def _build_batch_entry(resource: dict) -> tuple[dict, str, str | None]:
    """Return ``(bundle_entry, resourceType, source_id)`` for one resource.

    Sanitises the resource ID to be FHIR-R4-compliant (replaces underscores
    with hyphens, truncates to 64 chars) before building the PUT/POST entry.
    gPAS domains with underscore prefixes (e.g. ``rid_1234567890``) would
    otherwise be rejected by HAPI 7.x with HAPI-1364.
    """
    resource = _strip_manifest_tags(resource)
    rt = resource.get("resourceType") or "Unknown"
    source_id = resource.get("id")

    rid = source_id
    method = "POST"
    request_url = rt if _RESOURCE_TYPE_RE.match(rt or "") else "Basic"

    if rid:
        # Sanitise: replace invalid FHIR ID chars (e.g. underscores) with hyphens
        rid = _sanitise_resource_id(rid)
        if rid != source_id:
            log.debug("sanitised id %r -> %r for %s", source_id, rid, rt)
        resource = {**resource, "id": rid}
        if _RESOURCE_TYPE_RE.match(rt or ""):
            method = "PUT"
            request_url = f"{rt}/{rid}"

    entry = {
        "resource": resource,
        "request": {"method": method, "url": request_url},
    }
    return entry, rt, source_id


def _parse_batch_response(resp_bundle: dict, meta_list: list) -> list:
    """Extract per-entry success/failure from a FHIR batch-response Bundle."""
    results = []
    resp_entries = resp_bundle.get("entry", [])

    for j, (rt, source_id) in enumerate(meta_list):
        if j >= len(resp_entries):
            results.append(
                {
                    "resourceType": rt,
                    "source_id": source_id,
                    "server_id": None,
                    "success": False,
                    "error": "entry missing from batch-response Bundle",
                }
            )
            continue

        resp_meta = resp_entries[j].get("response", {})
        status_str = resp_meta.get("status", "")
        location = resp_meta.get("location", "")

        try:
            status_code = int(status_str.split()[0])
        except (ValueError, IndexError, AttributeError):
            status_code = 0

        success = 200 <= status_code < 300

        server_id = None
        if location:
            parts = location.split("/")
            if len(parts) >= 2:
                server_id = parts[1]

        if success:
            results.append(
                {
                    "resourceType": rt,
                    "source_id": source_id,
                    "server_id": server_id,
                    "success": True,
                    "error": None,
                }
            )
        else:
            # Extract OperationOutcome diagnostics from response.outcome
            outcome = resp_meta.get("outcome") or {}
            issues = outcome.get("issue", []) if isinstance(outcome, dict) else []
            diag = "; ".join(
                str(
                    iss.get("diagnostics")
                    or (iss.get("details") or {}).get("text")
                    or ""
                )
                for iss in issues
                if iss.get("diagnostics") or iss.get("details")
            )
            error_msg = f"HTTP {status_str}" + (f": {diag}" if diag else "")
            log.warning("upload_resources %s/%s: %s", rt, source_id or "?", error_msg)
            results.append(
                {
                    "resourceType": rt,
                    "source_id": source_id,
                    "server_id": None,
                    "success": False,
                    "error": error_msg,
                }
            )

    return results


def _post_bundle_batch(base: str, chunk: list[dict], token, timeout) -> list[dict]:
    """Build and POST one FHIR batch Bundle for *chunk*. Return a list of per-resource results.

    Used by both the serial and parallel paths of :func:`upload_resources`.
    Never raises — all errors are captured as per-resource result dicts.
    """
    entries = []
    meta_list: list[tuple[str, str | None]] = []
    results = []

    for resource in chunk:
        raw_rt = resource.get("resourceType") if resource else None
        if not raw_rt or not _RESOURCE_TYPE_RE.match(str(raw_rt)):
            source_id = resource.get("id") if resource else None
            results.append(
                {
                    "resourceType": str(raw_rt or "Unknown"),
                    "source_id": source_id,
                    "server_id": None,
                    "success": False,
                    "error": f"invalid or missing resourceType: {raw_rt!r}",
                }
            )
            continue
        entry, rt, source_id = _build_batch_entry(resource)
        entries.append(entry)
        meta_list.append((rt, source_id))

    if not entries:
        return results

    bundle = {"resourceType": "Bundle", "type": "batch", "entry": entries}
    try:
        resp_bundle = _write_json(
            base + "/",
            bundle,
            "POST",
            token=token,
            timeout=timeout,
            operation="batch_upload",
            target=True,
        )
    except ValueError as exc:
        log.warning("batch_upload chunk failed: %s", exc)
        for rt, source_id in meta_list:
            results.append(
                {
                    "resourceType": rt,
                    "source_id": source_id,
                    "server_id": None,
                    "success": False,
                    "error": str(exc),
                }
            )
        return results

    results.extend(_parse_batch_response(resp_bundle, meta_list))
    return results


def upload_resources(
    base_url,
    resources,
    token=None,
    timeout=30,
    parallel: int = 1,
    batch_size: int | None = None,
):
    """Upload FHIR resources to a server using FHIR batch Bundles.

    Consumes *resources* in two passes:

    1. **Tier inference pass**: Scans all resources to build a topological
       dependency graph and an ID-sanitisation map.
    2. **Upload pass**: Sorts by tier, rewrites cross-resource references,
       then sends chunks of *batch_size* (or ``_UPLOAD_BATCH_SIZE``)
       resources as FHIR batch Bundles via :func:`_post_bundle_batch`.

    When *parallel* > 1, bundles **within each topological tier** are posted
    concurrently via :class:`~concurrent.futures.ThreadPoolExecutor`.  Tiers
    are still uploaded sequentially (tier 0 completes before tier 1 starts),
    so FHIR referential integrity constraints are satisfied.  The default
    ``parallel=1`` retains the original sequential behaviour.

    Resource IDs are sanitised to FHIR R4 ``[A-Za-z0-9\\-.]{1,64}`` before
    upload — gPAS pseudonym prefixes like ``rid_`` become ``rid-``.

    Yields one result dict per resource:
        {
            "resourceType": str,
            "source_id":    str | None,
            "server_id":    str | None,
            "success":      bool,
            "error":        str | None,
        }

    Never raises — errors are captured per-resource or per-chunk.
    """
    chunk_size = batch_size or _UPLOAD_BATCH_SIZE
    base = base_url.rstrip("/")

    if not isinstance(resources, list):
        all_resources = list(resources)
    else:
        all_resources = resources

    if not all_resources:
        return

    # Compute topological upload order: tier 0 first (no deps), tier N last
    tiers = _infer_upload_tiers(all_resources)
    all_resources.sort(key=lambda r: tiers.get(r.get("resourceType", ""), 0))
    log.debug(
        "upload_resources: tier map: %s",
        {rt: t for rt, t in sorted(tiers.items(), key=lambda x: x[1])},
    )

    # Rewrite cross-resource references so they match the sanitised server IDs.
    id_map = _compute_id_map(all_resources)
    if id_map:
        log.debug(
            "upload_resources: rewriting %d changed ids in references", len(id_map)
        )
        all_resources = [_rewrite_references(r, id_map) for r in all_resources]

    total = len(all_resources)
    done = 0

    if parallel > 1:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Group resources into per-tier buckets (already sorted; order is preserved)
        tier_levels = sorted(
            set(
                tiers.get(r.get("resourceType", ""), 0)
                for r in all_resources
                if r is not None
            )
        )
        tier_buckets: dict[int, list[dict]] = {t: [] for t in tier_levels}
        for r in all_resources:
            if r is not None:
                tier_buckets[tiers.get(r.get("resourceType", ""), 0)].append(r)
        all_resources = None  # allow GC of sorted list; tier_buckets owns refs now

        for tier_level in tier_levels:
            tier_resources = tier_buckets.pop(tier_level)
            batches = [
                tier_resources[i : i + chunk_size]
                for i in range(0, len(tier_resources), chunk_size)
            ]
            effective = min(parallel, len(batches))
            with ThreadPoolExecutor(max_workers=effective) as pool:
                futs = [
                    pool.submit(_post_bundle_batch, base, b, token, timeout)
                    for b in batches
                ]
                # as_completed yields futures as they finish; pool.__exit__ (shutdown wait=True)
                # guarantees all tier-N futures complete before the next tier begins.
                for fut in as_completed(futs):
                    for result in fut.result():
                        yield result
                        done += 1
                        if done % 1000 == 0 or done == total:
                            log.info("upload_resources: %d/%d processed", done, total)
    else:
        for i in range(0, total, chunk_size):
            chunk = all_resources[i : i + chunk_size]
            # Free the consumed slice to reduce peak memory
            for j in range(i, min(i + chunk_size, total)):
                all_resources[j] = None

            for result in _post_bundle_batch(base, chunk, token, timeout):
                yield result
                done += 1

            if done % 1000 == 0 or done == total:
                log.info("upload_resources: %d/%d processed", done, total)
