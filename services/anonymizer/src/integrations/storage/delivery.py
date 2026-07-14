"""Per-destination S3 delivery of the finished de-identified file.

The internal result store (``store_result``) keeps a durable copy that powers
``GET /jobs/{id}/result``. Delivery is a separate, final step that pushes the same
file to the **dataspace S3 destination** chosen for the job  a per-job bucket +
credentials, unlike the single global result bucket.

Resolution order for the destination:

1. ``job.params["destination_id"]``  a saved, encrypted destination.
2. ``MEDANON_DEFAULT_DESTINATION_ID`` env  pin every job to one saved destination
   without per-request wiring.
3. otherwise ``None`` (no destination configured).

The always-S3 guarantee is enforced by the caller (:func:`publish_result`), not
here: when ``MEDANON_REQUIRE_S3_DELIVERY=true`` a job with no resolvable
destination fails closed rather than being left only in the internal store.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger("medanon.storage.delivery")

_S3_PREFIX = "s3://"


@dataclass(frozen=True)
class DestinationSpec:
    """Resolved, decrypted S3 destination for one delivery."""

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    key_prefix: str = ""
    region: str | None = None
    secure: bool = True
    path_style: bool = False


class DeliveryError(RuntimeError):
    """Raised when delivery to the dataspace S3 destination fails."""


def resolve_destination(job) -> DestinationSpec | None:
    """Resolve the S3 destination for *job* (from ``job.params['destination_id']``)."""
    return resolve_destination_spec((job.params or {}).get("destination_id"))


def resolve_destination_spec(dest_id: str | None) -> DestinationSpec | None:
    """Resolve an S3 destination by id, decrypting the secret transiently.

    Falls back to ``MEDANON_DEFAULT_DESTINATION_ID`` when *dest_id* is empty.
    Returns ``None`` when no destination is configured (the caller decides whether
    that is fatal). Raises if a referenced id exists but cannot be loaded/decrypted
     a misconfiguration must never silently downgrade to "no delivery".
    """
    dest_id = dest_id or os.environ.get("MEDANON_DEFAULT_DESTINATION_ID")
    if not dest_id:
        return None

    from integrations.connectors import get_destination_store

    store = get_destination_store()
    if store is None:
        raise DeliveryError(
            "destination_id set but the destination store is not initialised  "
            "set MEDANON_APP_DB_URL (PostgreSQL) to use saved S3 destinations."
        )
    meta = store.get(dest_id)
    if meta is None:
        raise DeliveryError(f"output destination '{dest_id}' not found")
    enc = store.get_encrypted_secret(dest_id)
    if not enc:
        raise DeliveryError(f"output destination '{dest_id}' has no stored secret key")

    from integrations.sql_source.secrets import decrypt_secret

    return DestinationSpec(
        endpoint=meta["endpoint"],
        bucket=meta["bucket"],
        access_key=meta["access_key"],
        secret_key=decrypt_secret(enc),
        key_prefix=meta.get("key_prefix") or "",
        region=meta.get("region"),
        secure=bool(meta.get("secure", True)),
        path_style=bool(meta.get("path_style", False)),
    )


def _local_suffix(local_path: str) -> str:
    """Return the meaningful file suffix, treating ``.ndjson.gz`` as one unit."""
    name = os.path.basename(local_path)
    if name.endswith(".ndjson.gz"):
        return ".ndjson.gz"
    return Path(name).suffix or ".ndjson"


# The three released artifacts each get their own top-level segment under the
# destination's key_prefix so operators can scope IAM/bucket policies per type
# (e.g. grant the de-identified data broadly but restrict manifests + audit to
# auditors). All three share the same <stem> (the job id) so they correlate and
# are trivial to find together.
_ARTIFACT_SEGMENTS = {"data": "data", "manifest": "manifests", "audit": "audit"}
# manifest/audit carry a canonical suffix; data keeps its real file suffix.
_ARTIFACT_SUFFIX = {"manifest": ".manifest.ndjson", "audit": ".audit.json"}


def build_object_key(
    spec: DestinationSpec, job, local_path: str, artifact: str = "data"
) -> str:
    """Render the S3 object key for one *artifact* of *job*.

    Layout: ``<key_prefix>/<segment>/<stem><suffix>`` where ``segment`` is
    ``data`` / ``manifests`` / ``audit`` and ``stem`` is the job id. The
    destination ``key_prefix`` is treated as a directory prefix and supports the
    tokens ``{job_id}``, ``{permit_id}``, ``{ts}``, ``{resource_type}``.
    """
    params = job.params or {}
    segment = _ARTIFACT_SEGMENTS.get(artifact, "data")
    suffix = _ARTIFACT_SUFFIX.get(artifact) or _local_suffix(local_path)
    # A canonical-suffix artifact compressed on the way out keeps its ``.gz`` so
    # the object key describes what is actually stored.
    if artifact in _ARTIFACT_SUFFIX and local_path.endswith(".gz"):
        suffix += ".gz"
    name = f"{job.id}{suffix}"

    tokens = {
        "job_id": job.id,
        "permit_id": params.get("permit_id") or "no-permit",
        "resource_type": params.get("resource_type") or "all",
        "ts": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    }
    try:
        rendered = (spec.key_prefix or "").format(**tokens).strip().lstrip("/")
    except (KeyError, IndexError, ValueError) as exc:
        raise DeliveryError(
            f"invalid key_prefix template {spec.key_prefix!r}: {exc}"
        ) from exc

    prefix = rendered.rstrip("/")
    return "/".join(p for p in (prefix, segment, name) if p)


def deliver(local_path: str, spec: DestinationSpec, object_key: str) -> str:
    """Upload *local_path* to the destination bucket. Returns the ``s3://`` key."""
    from integrations.storage.s3 import make_minio_client, put_file

    try:
        client = make_minio_client(
            spec.endpoint,
            spec.access_key,
            spec.secret_key,
            secure=spec.secure,
            region=spec.region,
        )
        if not client.bucket_exists(spec.bucket):
            client.make_bucket(spec.bucket)
        size = put_file(client, spec.bucket, object_key, local_path)
    except Exception as exc:
        raise DeliveryError(
            f"delivery to s3://{spec.bucket}/{object_key} failed: {exc}"
        ) from exc

    key = f"{_S3_PREFIX}{spec.bucket}/{object_key}"
    _log.info("delivery_done key=%s size_bytes=%d", key, size)
    return key


def _require_delivery() -> bool:
    return os.environ.get("MEDANON_REQUIRE_S3_DELIVERY", "false").lower() == "true"


def deliver_content(
    data: bytes | str,
    *,
    suffix: str,
    destination_id: str | None = None,
    params: dict | None = None,
    key_id: str | None = None,
    artifact: str = "data",
) -> str | None:
    """Write *data* to a temp file and deliver it to the resolved S3 destination.

    The delivery counterpart of :func:`~integrations.storage.publish_result` for
    synchronous (non-job) outputs  the format endpoints (DICOM / CDA / HL7 v2)
    that return content inline instead of producing a job result file.

    Resolution + policy match the job path: the destination comes from
    *destination_id* or ``MEDANON_DEFAULT_DESTINATION_ID``; when none resolves and
    ``MEDANON_REQUIRE_S3_DELIVERY=true`` this raises (fail-closed), otherwise it
    returns ``None`` (nothing delivered). Returns the delivered ``s3://`` key.

    *suffix* is the object extension (e.g. ``.dcm``, ``.xml``, ``.hl7``, ``.zip``);
    *params* feeds the destination key-prefix template (``{permit_id}``,
    ``{resource_type}``, ``{ts}``, ``{job_id}``).
    """
    import tempfile
    import uuid

    spec = resolve_destination_spec(destination_id)
    if spec is None:
        if _require_delivery():
            raise DeliveryError(
                "MEDANON_REQUIRE_S3_DELIVERY=true but no S3 output destination "
                "resolved (pass destination_id or set MEDANON_DEFAULT_DESTINATION_ID)"
            )
        return None

    kid = key_id or uuid.uuid4().hex
    payload = data if isinstance(data, bytes) else data.encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix=f"deliver_{kid}_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        # Reuse the job-shaped key builder via a lightweight stand-in.
        from types import SimpleNamespace

        job_like = SimpleNamespace(id=kid, params=params or {})
        object_key = build_object_key(spec, job_like, tmp_path, artifact=artifact)
        return deliver(tmp_path, spec, object_key)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
