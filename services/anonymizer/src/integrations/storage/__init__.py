"""Result storage — local filesystem or S3-compatible object store (MinIO).

Backends:
    ``local`` (default) — writes NDJSON to ``/output/{job_id}.ndjson`` on disk.
    ``s3``               — uploads to MinIO/S3; result key is ``s3://bucket/object``.

Usage::

    from integrations.storage import get_result_storage

    storage = get_result_storage()
    key   = storage.write_from_path(job_id, local_path)   # returns result key
    url   = storage.get_download_url(key)                  # presigned URL or None
    valid = storage.exists(key)
    with storage.open_stream(key) as stream:
        for line in stream: ...

Selected by ``MEDANON_RESULT_STORAGE`` env var (``local`` / ``s3``).
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.storage")

_storage = None


def get_result_storage():
    """Return the configured result storage backend (lazy-init on first call)."""
    global _storage
    if _storage is None:
        _storage = _init_from_env()
    return _storage


def configure_result_storage(storage) -> None:
    """Override the storage backend — used for testing or explicit startup init."""
    global _storage
    _storage = storage


def _init_from_env():
    mode = os.environ.get("MEDANON_RESULT_STORAGE", "local").lower().strip()
    if mode == "s3":
        from integrations.storage.s3 import S3ResultStorage

        endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
        access_key = os.environ.get("MINIO_ROOT_USER", "minioadmin")
        secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
        bucket = os.environ.get("MINIO_BUCKET", "medanon-results")
        secure = os.environ.get("MINIO_SECURE", "false").lower() == "true"
        storage = S3ResultStorage(
            endpoint, access_key, secret_key, bucket, secure=secure
        )
        _log.info("result_storage=s3 endpoint=%s bucket=%s", endpoint, bucket)
        return storage
    from integrations.storage.local import LocalResultStorage

    _log.info("result_storage=local")
    return LocalResultStorage()


def delete_result(result_key: str) -> bool:
    """Delete a stored result by key (local path or s3:// key).

    Returns True if the delete succeeded, False on error (best-effort).
    """
    try:
        return get_result_storage().delete(result_key)
    except Exception as exc:
        _log.warning("delete_result_failed key=%s: %s", result_key, exc)
        return False


def store_result(job_id: str, local_path: str) -> str:
    """Upload *local_path* to the configured result storage and return the result key.

    For local storage (default) the key is the file path unchanged.
    For S3 (``MEDANON_RESULT_STORAGE=s3``) the file is uploaded to MinIO and the
    key is ``s3://<bucket>/<job_id>.ndjson``.
    """
    return get_result_storage().write_from_path(job_id, local_path)


def publish_result(
    job,
    local_path: str,
    *,
    manifest_path: str | None = None,
    audit: dict | None = None,
) -> str:
    """Finalize a de-identified export: durable internal copy + dataspace delivery.

    1. ``store_result`` writes the durable internal copy that powers
       ``GET /jobs/{id}/result`` (unchanged behaviour, returned as the result key).
    2. **Three correlated artifacts** are delivered to the job's resolved **S3
       output destination**, each under its own prefix but a shared job-id stem
       (``data/`` ``manifests/`` ``audit/``) so they can carry different access
       controls yet are easy to find together:
         - the de-identified clinical **data** (``local_path``);
         - the transformation **manifest** sidecar, when ``manifest_path`` is given
           (FHIR-resource exports produce one; tabular/SQL/DICOM do not);
         - an **audit** JSON built from ``audit`` (else the job's stored summary).
       The delivered ``s3://`` keys are recorded on ``job.params["delivered_to"]``
       as ``{"data":…, "manifest":…, "audit":…}``.
    3. Fail-closed: when ``MEDANON_REQUIRE_S3_DELIVERY=true`` a job with no
       resolvable destination — or a failed delivery — raises so the job fails,
       guaranteeing the artifacts always land in S3. When the flag is off, a
       missing/failed destination is a non-fatal warning and only the internal
       copy is kept.

    4. **Score gate (fail-closed choke point).** Every export path funnels through
       here, so the gate runs here rather than in each executor. The staged path
       (the one that actually runs for patient exports), the SQL path and the
       tabular path never called it, and a new export path would have inherited
       that hole by default. Blocked jobs raise ``ScoreGateBlocked`` *before*
       ``store_result``, so nothing is ever written to the durable store or the
       S3 destination and there is no write-then-delete window.
    """
    _enforce_score_gate(job, local_path, manifest_path, audit)

    # Universal manifest split: paths that did not pre-produce a manifest sidecar
    # (e.g. the staged large-export path) get their embedded manifest extracted
    # into a separate artifact here, and the data file rewritten clean — so EVERY
    # export type releases the transformation manifest separately. Runs before
    # store_result so the internal copy and the delivered copy are both clean.
    if manifest_path is None and local_path.endswith(".ndjson"):
        try:
            from pipeline.manifest import split_ndjson_manifest

            manifest_path = split_ndjson_manifest(local_path)
        except Exception:
            manifest_path = None

    key = store_result(job.id, local_path)

    require = os.environ.get("MEDANON_REQUIRE_S3_DELIVERY", "false").lower() == "true"
    try:
        _publish_artifacts(job, local_path, key, manifest_path, audit, require)
    finally:
        # The manifest sidecar is transient (delivered above, not served by GET
        # /result) — remove it whether or not delivery ran.
        if manifest_path and os.path.exists(manifest_path):
            try:
                os.unlink(manifest_path)
            except OSError:
                pass
    return key


def _compress_manifest() -> bool:
    return os.environ.get("MEDANON_COMPRESS_MANIFEST", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _maybe_gzip(path: str) -> str:
    """Gzip *path* to a sibling ``.gz`` and return it; return *path* unchanged on opt-out.

    The manifest sidecar is one line per released resource and is almost pure
    repetition: on a 131k-resource export it measured 104.6 MB raw with only 195
    distinct (rule, action, path) triples, and gzip -6 compressed it ~65x. That
    turns the largest delivered artifact into the smallest at no semantic cost —
    it stays newline-delimited JSON, just compressed.

    Streamed through ``copyfileobj`` so a multi-hundred-MB sidecar never lands in
    memory. Best-effort: on any failure the raw file is delivered.

    The output is byte-reproducible for a given input: ``mtime=0`` suppresses the
    timestamp and ``filename=""`` suppresses the gzip FNAME header field, which
    would otherwise embed the job-id-bearing source name and make two identical
    manifests hash differently. An audit artifact should hash the same for the
    same content.
    """
    if not _compress_manifest() or path.endswith(".gz"):
        return path

    import gzip
    import shutil

    gz_path = f"{path}.gz"
    try:
        with (
            open(path, "rb") as src,
            open(gz_path, "wb") as raw,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0
            ) as dst,
        ):
            shutil.copyfileobj(src, dst, length=1 << 20)
    except Exception:
        _log.warning(
            "manifest_gzip_failed path=%s — delivering raw", path, exc_info=True
        )
        try:
            os.unlink(gz_path)
        except OSError:
            pass
        return path

    raw, comp = os.path.getsize(path), os.path.getsize(gz_path)
    _log.info(
        "manifest_compressed raw_bytes=%d gz_bytes=%d ratio=%.1fx",
        raw,
        comp,
        (raw / comp) if comp else 1.0,
    )
    return gz_path


def _enforce_score_gate(job, local_path, manifest_path, audit) -> None:
    """Run the score gate before anything is promoted. Raises ``ScoreGateBlocked``.

    ``check_score_gate`` is a no-op when the gate is disabled, when scoring is
    off, or when *audit* carries no computed score — so paths that legitimately
    have no score (bulk-import, empty exports) pass straight through.

    On a block the local staging files are removed and ``job.result_path`` is
    cleared, mirroring ``_cleanup_blocked_output``. The executor may also hold a
    score-audit markdown file; it catches the same exception and cleans that up.
    """
    score = (audit or {}).get("score")
    if not score:
        return

    from pipeline.scoring.gate import ScoreGateBlocked, check_score_gate

    profile = "auto"
    if isinstance(getattr(job, "params", None), dict):
        profile = job.params.get("config_profile") or "auto"

    try:
        check_score_gate(score, profile)
    except ScoreGateBlocked:
        _log.warning("score_gate_blocked job=%s — output withheld", job.id)
        for path in (local_path, manifest_path):
            if not path:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
        job.result_path = None
        raise


def _publish_artifacts(job, local_path, key, manifest_path, audit, require) -> None:
    """Deliver data + manifest + audit to the resolved S3 destination."""
    from integrations.storage.delivery import (
        DeliveryError,
        build_object_key,
        deliver,
        resolve_destination,
    )

    try:
        spec = resolve_destination(job)
        if spec is None:
            if require:
                raise DeliveryError(
                    f"MEDANON_REQUIRE_S3_DELIVERY=true but no S3 output destination "
                    f"resolved for job {job.id} (set destination_id or "
                    f"MEDANON_DEFAULT_DESTINATION_ID)"
                )
            # Silence here is what makes a missing manifest/audit look like a
            # delivery bug rather than a missing destination. Say so explicitly.
            _log.warning(
                "delivery_skipped job=%s reason=no_destination internal_key=%s "
                "(no destination_id and no MEDANON_DEFAULT_DESTINATION_ID; "
                "data + manifest + audit were NOT delivered to S3)",
                job.id,
                key,
            )
            return

        delivered: dict[str, str] = {}
        # 1. de-identified clinical data
        delivered["data"] = deliver(
            local_path, spec, build_object_key(spec, job, local_path, artifact="data")
        )
        # 2. transformation manifest (FHIR exports only), gzipped by default
        if manifest_path and os.path.exists(manifest_path):
            upload_path = _maybe_gzip(manifest_path)
            try:
                delivered["manifest"] = deliver(
                    upload_path,
                    spec,
                    build_object_key(spec, job, upload_path, artifact="manifest"),
                )
            finally:
                if upload_path != manifest_path:
                    try:
                        os.unlink(upload_path)
                    except OSError:
                        pass
        # 3. audit record (always) — built here so every path gets one
        delivered["audit"] = _deliver_audit(job, spec, audit, delivered)

        if isinstance(job.params, dict):
            job.params["delivered_to"] = delivered
        _log.info(
            "publish_result job=%s internal=%s delivered=%s", job.id, key, delivered
        )
    except DeliveryError:
        if require:
            raise
        _log.warning("delivery_skipped_nonfatal job=%s", job.id, exc_info=True)


def _deliver_audit(job, spec, audit: dict | None, delivered: dict) -> str:
    """Build the audit JSON and upload it under the ``audit/`` prefix."""
    import tempfile

    from integrations.storage.delivery import build_object_key, deliver
    from pipeline.jobs.audit_artifact import build_audit
    from utils.json_fast import dumps as _json_dumps

    record = build_audit(job, summary=audit, delivered=delivered)
    payload = _json_dumps(record).encode("utf-8")
    fd, tmp = tempfile.mkstemp(suffix=".audit.json", prefix=f"audit_{job.id}_")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        return deliver(tmp, spec, build_object_key(spec, job, tmp, artifact="audit"))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
