"""Result-publishing orchestration: the fail-closed finalisation of an export.

Lifted out of ``integrations/storage`` because it is data-pipeline orchestration,
not I/O: it runs the score gate (fail-closed choke point), splits the manifest,
writes the durable internal copy, and delivers the correlated data/manifest/audit
artifacts. It *uses* the storage adapter (``store_result``, ``deliver``) for the
actual byte-writing, so the adapter stays pure I/O and this module is where the
"what to publish, in what order, gated by what" decisions live.

Imports of the storage adapter and pipeline helpers are kept function-local:
they defer the adapter load and, deliberately, let tests monkeypatch the source
modules (``integrations.storage.store_result``, ``...delivery.deliver``,
``pipeline.scoring.gate.check_score_gate``) and have this orchestration pick up
the patched versions at call time.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.result_publisher")


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
       resolvable destination  or a failed delivery  raises so the job fails,
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
    # into a separate artifact here, and the data file rewritten clean  so EVERY
    # export type releases the transformation manifest separately. Runs before
    # store_result so the internal copy and the delivered copy are both clean.
    if manifest_path is None and local_path.endswith(".ndjson"):
        try:
            from pipeline.manifest import split_ndjson_manifest

            manifest_path = split_ndjson_manifest(local_path)
        except Exception:
            manifest_path = None

    from integrations.storage import store_result

    key = store_result(job.id, local_path)

    require = os.environ.get("MEDANON_REQUIRE_S3_DELIVERY", "false").lower() == "true"
    try:
        _publish_artifacts(job, local_path, key, manifest_path, audit, require)
    finally:
        # The manifest sidecar is transient (delivered above, not served by GET
        # /result)  remove it whether or not delivery ran.
        if manifest_path and os.path.exists(manifest_path):
            try:
                os.unlink(manifest_path)
            except OSError:
                pass
    return key


def _enforce_score_gate(job, local_path, manifest_path, audit) -> None:
    """Run the score gate before anything is promoted. Raises ``ScoreGateBlocked``.

    ``check_score_gate`` is a no-op when the gate is disabled, when scoring is
    off, or when *audit* carries no computed score  so paths that legitimately
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
        _log.warning("score_gate_blocked job=%s  output withheld", job.id)
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
    from integrations.storage import _maybe_gzip
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
        # 3. audit record (always)  built here so every path gets one
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
