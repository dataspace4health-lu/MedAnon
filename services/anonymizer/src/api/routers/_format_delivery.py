"""Shared helper: deliver a synchronous format-endpoint output to S3.

The DICOM / CDA / HL7 v2 endpoints return de-identified content inline. When a
dataspace S3 destination is configured (a ``?destination_id=`` query param or
``MEDANON_DEFAULT_DESTINATION_ID``), the same content is also written to a file
and delivered to that bucket, exactly like the async export jobs. The delivered
``s3://`` key is returned so the caller can surface it as an ``X-Delivered-To``
response header.

Fail-closed: with ``MEDANON_REQUIRE_S3_DELIVERY=true`` and no destination, the
request fails (502) rather than silently returning content that never reached S3.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, Request


async def deliver_format_output(
    data: bytes | str,
    *,
    suffix: str,
    request: Request,
    resource_type: str,
    permit_id: str | None = None,
    manifest: list[dict] | None = None,
) -> str | None:
    """Deliver *data* (+ its transformation manifest + an audit record) to S3.

    Three correlated artifacts under distinct prefixes with a shared stem, exactly
    like the async export jobs: the de-identified content (``data``), the
    transformation ``manifest`` (a list of format-specific ``{...}`` entries, when
    provided), and an audit record. Returns the data ``s3://`` key, or ``None``
    when no destination is configured (and delivery is not required).
    """
    import json
    import uuid
    from types import SimpleNamespace

    from integrations.storage.delivery import DeliveryError, deliver_content
    from pipeline.jobs.audit_artifact import build_audit

    destination_id = request.query_params.get("destination_id") or None
    params = {"resource_type": resource_type}
    if permit_id:
        params["permit_id"] = permit_id
    stem = uuid.uuid4().hex
    fmt = resource_type.lower()
    try:
        data_key = await asyncio.to_thread(
            deliver_content,
            data,
            suffix=suffix,
            destination_id=destination_id,
            params=params,
            key_id=stem,
            artifact="data",
        )
        if data_key is None:
            return None  # no destination configured — nothing delivered
        delivered = {"data": data_key}
        if manifest is not None:
            man_line = json.dumps({"format": fmt, "transformations": manifest}) + "\n"
            delivered["manifest"] = await asyncio.to_thread(
                deliver_content,
                man_line,
                suffix=".manifest.ndjson",
                destination_id=destination_id,
                params=params,
                key_id=stem,
                artifact="manifest",
            )
        job_like = SimpleNamespace(
            id=stem,
            type=f"format-{fmt}",
            params={**params, "config_profile": fmt},
            status=None,
            created_at=None,
            checkpoint_data=None,
        )
        audit_doc = build_audit(job_like, delivered=delivered)
        await asyncio.to_thread(
            deliver_content,
            json.dumps(audit_doc),
            suffix=".audit.json",
            destination_id=destination_id,
            params=params,
            key_id=stem,
            artifact="audit",
        )
        return data_key
    except DeliveryError as exc:
        raise HTTPException(
            status_code=502, detail=f"S3 delivery failed: {exc}"
        ) from exc
