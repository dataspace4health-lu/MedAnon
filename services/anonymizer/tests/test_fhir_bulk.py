"""Tests for FHIR R4 Bulk Data Access IG endpoints (api/routers/fhir_bulk.py).

Covers:
  - System, patient, and group export trigger endpoints (GET /$export etc.)
  - Status polling: pending, done, error, not-found
  - Job cancellation (DELETE /fhir/export-status/{job_id})
  - Query-parameter propagation (_type, _since)
  - _outputFormat validation

Runs locally — no Docker required. Uses FastAPI TestClient with the job store
patched at the module level so no SQLite database is needed.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal env bootstrap — must happen before any app import
# ---------------------------------------------------------------------------
os.environ.setdefault("MEDANON_API_KEY", "")          # open mode for tests
os.environ.setdefault("MEDANON_HASH_ALLOW_PLAIN", "true")
os.environ.setdefault("MEDANON_RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
os.environ.setdefault("MEDANON_CONFIG_DIR", _CONFIG_DIR)

# ---------------------------------------------------------------------------
# typing.io shim (fhirpathpy / antlr4 compat on Python 3.13)
# ---------------------------------------------------------------------------
import types
import typing

if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO
    _io_mod.TextIO = typing.TextIO
    _io_mod.BinaryIO = typing.BinaryIO
    sys.modules["typing.io"] = _io_mod


# ---------------------------------------------------------------------------
# App + client factory
# ---------------------------------------------------------------------------

def _make_client(extra_env: dict | None = None):
    """Build a TestClient for the full FastAPI app with optional env overrides.

    Always mounts the fhir_bulk router at /fhir.
    """
    env = {
        "MEDANON_CONFIG_DIR": _CONFIG_DIR,
        "MEDANON_API_KEY": "",
        "MEDANON_RATE_LIMIT_ENABLED": "false",
        "MEDANON_HASH_ALLOW_PLAIN": "true",
        "GPAS_URL": "",
        "FHIR_SOURCE_URL": "http://fhir-server:8080/fhir",
        "LOG_LEVEL": "WARNING",
    }
    if extra_env:
        env.update(extra_env)

    with patch.dict(os.environ, env, clear=False):
        # Force fresh import so env vars and module-level singletons are reset
        for mod in list(sys.modules):
            if mod in ("api.auth", "api.main", "api.routers.fhir_bulk", "api.deps"):
                del sys.modules[mod]

        from api.main import app
        from api.routers import fhir_bulk as _fhir_bulk_module

        # Mount bulk router at /fhir (idempotent — skip if already mounted)
        already_mounted = any(
            getattr(r, "path", None) == "/fhir"
            for r in getattr(app, "routes", [])
        )
        if not already_mounted:
            app.include_router(_fhir_bulk_module.router, prefix="/fhir")

        from fastapi.testclient import TestClient
        return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Shared mock job helpers
# ---------------------------------------------------------------------------

def _make_mock_job(
    job_id: str = "test-job-123",
    status_value: str = "pending",
    params: dict | None = None,
    updated_at: str = "2023-01-01T00:00:00+00:00",
    error: str | None = None,
    result_path: str | None = None,
) -> MagicMock:
    """Return a MagicMock that looks like a Job domain object."""
    job = MagicMock()
    job.id = job_id
    job.status.value = status_value
    # Make job.status compare equal to JobStatus enum members by value
    from medanon_core.domain import JobStatus
    job.status = JobStatus(status_value)
    job.params = params or {"request_url": "http://test/fhir/$export"}
    job.updated_at = updated_at
    job.error = error
    job.result_path = result_path
    return job


def _make_mock_store(job: MagicMock) -> MagicMock:
    """Return a MagicMock store whose create() and get() return *job*."""
    store = MagicMock()
    store.create.return_value = job
    store.get.return_value = job
    store.notify_new_job.return_value = None
    store.update.return_value = None
    return store


# ===========================================================================
# Test classes
# ===========================================================================


class TestSystemExport(unittest.TestCase):
    """GET /fhir/$export — system-level export trigger."""

    def setUp(self):
        self._job = _make_mock_job()
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_system_export_returns_202(self):
        resp = self.client.get("/fhir/$export")
        self.assertEqual(resp.status_code, 202)

    def test_system_export_content_location_header(self):
        resp = self.client.get("/fhir/$export")
        location = resp.headers.get("content-location", "")
        self.assertIn("/fhir/export-status/", location)

    def test_type_filter_stored_in_job(self):
        self.client.get("/fhir/$export?_type=Patient,Observation")
        call_kwargs = self._store.create.call_args
        params = call_kwargs[0][1]  # second positional arg to create()
        self.assertEqual(params.get("type_filter"), "Patient,Observation")

    def test_since_filter_stored_in_job(self):
        since = "2023-01-01T00:00:00Z"
        self.client.get(f"/fhir/$export?_since={since}")
        params = self._store.create.call_args[0][1]
        self.assertEqual(params.get("since"), since)

    def test_export_wrong_format_returns_422(self):
        resp = self.client.get("/fhir/$export?_outputFormat=application/xml")
        self.assertEqual(resp.status_code, 422)

    def test_export_missing_fhir_source_url_returns_422(self):
        with patch.dict(os.environ, {"FHIR_SOURCE_URL": ""}, clear=False):
            # Re-create client without FHIR_SOURCE_URL
            resp = _make_client(extra_env={"FHIR_SOURCE_URL": ""}).get(
                "/fhir/$export"
            )
        self.assertEqual(resp.status_code, 422)


class TestPatientExport(unittest.TestCase):
    """GET /fhir/Patient/$export — patient-level export trigger."""

    def setUp(self):
        self._job = _make_mock_job()
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_patient_export_returns_202(self):
        resp = self.client.get("/fhir/Patient/$export")
        self.assertEqual(resp.status_code, 202)

    def test_patient_level_stored_in_job(self):
        self.client.get("/fhir/Patient/$export")
        params = self._store.create.call_args[0][1]
        self.assertEqual(params.get("level"), "patient")


class TestGroupExport(unittest.TestCase):
    """GET /fhir/Group/{group_id}/$export — group-level export trigger."""

    def setUp(self):
        self._job = _make_mock_job()
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_group_export_returns_202(self):
        resp = self.client.get("/fhir/Group/cohort1/$export")
        self.assertEqual(resp.status_code, 202)

    def test_group_id_stored_as_resource_type(self):
        self.client.get("/fhir/Group/cohort1/$export")
        params = self._store.create.call_args[0][1]
        self.assertEqual(params.get("resource_type"), "cohort1")
        self.assertEqual(params.get("level"), "group")


class TestExportStatusPending(unittest.TestCase):
    """GET /fhir/export-status/{job_id} — pending job."""

    def setUp(self):
        self._job = _make_mock_job(status_value="pending")
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_poll_pending_returns_202(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 202)

    def test_poll_pending_has_x_progress_header(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        x_progress = resp.headers.get("x-progress", "")
        self.assertIn("pending", x_progress)
        self.assertIn("test-job-123", x_progress)


class TestExportStatusRunning(unittest.TestCase):
    """GET /fhir/export-status/{job_id} — running job."""

    def setUp(self):
        self._job = _make_mock_job(status_value="running")
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_poll_running_returns_202(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 202)

    def test_poll_running_has_x_progress_header(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        x_progress = resp.headers.get("x-progress", "")
        self.assertIn("running", x_progress)


class TestExportStatusDone(unittest.TestCase):
    """GET /fhir/export-status/{job_id} — completed job."""

    def setUp(self):
        self._job = _make_mock_job(
            status_value="done",
            result_path=None,  # no file — tests fallback Bundle output
        )
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_poll_done_returns_200_manifest(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 200)

    def test_poll_done_response_is_json(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        self.assertEqual(resp.headers.get("content-type", "").split(";")[0], "application/json")

    def test_poll_done_manifest_has_output(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        body = resp.json()
        self.assertIn("output", body)
        self.assertIsInstance(body["output"], list)
        self.assertGreater(len(body["output"]), 0)

    def test_poll_done_manifest_has_transaction_time(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        body = resp.json()
        self.assertIn("transactionTime", body)
        self.assertEqual(body["transactionTime"], "2023-01-01T00:00:00+00:00")

    def test_poll_done_manifest_has_request_url(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        body = resp.json()
        self.assertIn("request", body)
        self.assertEqual(body["request"], "http://test/fhir/$export")

    def test_poll_done_manifest_output_has_type_and_url(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        first = resp.json()["output"][0]
        self.assertIn("type", first)
        self.assertIn("url", first)
        self.assertIn("/v1/jobs/", first["url"])

    def test_poll_done_manifest_output_uses_bundle_fallback_when_no_file(self):
        """When result_path is None the output list should fall back to Bundle."""
        resp = self.client.get("/fhir/export-status/test-job-123")
        first = resp.json()["output"][0]
        self.assertEqual(first["type"], "Bundle")

    def test_poll_done_manifest_output_uses_resource_types_from_file(self):
        """When result_path points to an NDJSON file the resource types are inferred."""
        import tempfile

        lines = [
            json.dumps({"resourceType": "Patient", "id": "p1"}),
            json.dumps({"resourceType": "Observation", "id": "o1"}),
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ndjson", delete=False
        ) as fh:
            fh.write("\n".join(lines) + "\n")
            tmp_path = fh.name

        try:
            job_with_file = _make_mock_job(
                status_value="done",
                result_path=tmp_path,
            )
            store_with_file = _make_mock_store(job_with_file)
            with patch("pipeline.jobs.store._job_store", store_with_file):
                client = _make_client()
                resp = client.get("/fhir/export-status/test-job-123")

            self.assertEqual(resp.status_code, 200)
            output = resp.json()["output"]
            types_in_output = [o["type"] for o in output]
            self.assertIn("Patient", types_in_output)
            self.assertIn("Observation", types_in_output)
        finally:
            os.unlink(tmp_path)


class TestExportStatusError(unittest.TestCase):
    """GET /fhir/export-status/{job_id} — failed job."""

    def setUp(self):
        self._job = _make_mock_job(
            status_value="error",
            error="Connection refused",
        )
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_poll_error_returns_500(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 500)

    def test_poll_error_body_is_operation_outcome(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        body = resp.json()
        self.assertEqual(body.get("resourceType"), "OperationOutcome")
        self.assertIn("issue", body)
        self.assertTrue(len(body["issue"]) > 0)

    def test_poll_error_diagnostics_contains_message(self):
        resp = self.client.get("/fhir/export-status/test-job-123")
        body = resp.json()
        diagnostics = body["issue"][0].get("diagnostics", "")
        self.assertIn("Connection refused", diagnostics)


class TestExportStatusNotFound(unittest.TestCase):
    """GET /fhir/export-status/{job_id} — unknown job."""

    def setUp(self):
        self._store = MagicMock()
        self._store.get.return_value = None
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_poll_notfound_returns_404(self):
        resp = self.client.get("/fhir/export-status/nonexistent-id")
        self.assertEqual(resp.status_code, 404)

    def test_poll_notfound_body_is_operation_outcome(self):
        resp = self.client.get("/fhir/export-status/nonexistent-id")
        body = resp.json()
        self.assertEqual(body.get("resourceType"), "OperationOutcome")


class TestCancelExport(unittest.TestCase):
    """DELETE /fhir/export-status/{job_id} — job cancellation."""

    def _make_client_with_job(self, job: MagicMock) -> object:
        store = _make_mock_store(job)
        self._store = store
        self._patcher = patch("pipeline.jobs.store._job_store", store)
        self._patcher.start()
        return _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_delete_pending_job_returns_202(self):
        job = _make_mock_job(status_value="pending")
        client = self._make_client_with_job(job)
        resp = client.delete("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 202)

    def test_delete_pending_job_updates_store(self):
        job = _make_mock_job(status_value="pending")
        client = self._make_client_with_job(job)
        client.delete("/fhir/export-status/test-job-123")
        self._store.update.assert_called_once()
        # Verify error message was set on the job
        self.assertIn("Cancelled", job.error)

    def test_delete_running_job_returns_202(self):
        job = _make_mock_job(status_value="running")
        client = self._make_client_with_job(job)
        resp = client.delete("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 202)

    def test_delete_done_job_returns_409(self):
        job = _make_mock_job(status_value="done")
        client = self._make_client_with_job(job)
        resp = client.delete("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 409)

    def test_delete_error_job_returns_409(self):
        job = _make_mock_job(status_value="error", error="previous failure")
        client = self._make_client_with_job(job)
        resp = client.delete("/fhir/export-status/test-job-123")
        self.assertEqual(resp.status_code, 409)

    def test_delete_notfound_returns_404(self):
        store = MagicMock()
        store.get.return_value = None
        self._patcher = patch("pipeline.jobs.store._job_store", store)
        self._patcher.start()
        client = _make_client()
        resp = client.delete("/fhir/export-status/nonexistent-id")
        self.assertEqual(resp.status_code, 404)


class TestFhirBulkSpecCompliance(unittest.TestCase):
    """Additional compliance checks: spec fields, bulk-spec flag, etc."""

    def setUp(self):
        self._job = _make_mock_job()
        self._store = _make_mock_store(self._job)
        self._patcher = patch("pipeline.jobs.store._job_store", self._store)
        self._patcher.start()
        self.client = _make_client()

    def tearDown(self):
        self._patcher.stop()

    def test_fhir_bulk_spec_flag_stored_in_job(self):
        """Jobs created by spec endpoints must carry _fhir_bulk_spec=True."""
        self.client.get("/fhir/$export")
        params = self._store.create.call_args[0][1]
        self.assertTrue(params.get("_fhir_bulk_spec"))

    def test_request_url_stored_in_job(self):
        """The original request URL must be stored so the manifest can reference it."""
        self.client.get("/fhir/$export")
        params = self._store.create.call_args[0][1]
        self.assertIn("request_url", params)
        self.assertIn("$export", params["request_url"])

    def test_content_location_points_to_export_status(self):
        resp = self.client.get("/fhir/Patient/$export")
        location = resp.headers.get("content-location", "")
        self.assertRegex(location, r"/fhir/export-status/[^/]+$")

    def test_done_manifest_requires_access_token_false_by_default(self):
        job = _make_mock_job(status_value="done")
        store = _make_mock_store(job)
        with patch("pipeline.jobs.store._job_store", store):
            client = _make_client()
            resp = client.get("/fhir/export-status/test-job-123")
        self.assertFalse(resp.json().get("requiresAccessToken", True))

    def test_done_manifest_has_error_and_deleted_arrays(self):
        job = _make_mock_job(status_value="done")
        store = _make_mock_store(job)
        with patch("pipeline.jobs.store._job_store", store):
            client = _make_client()
            resp = client.get("/fhir/export-status/test-job-123")
        body = resp.json()
        self.assertIn("error", body)
        self.assertIsInstance(body["error"], list)
        self.assertIn("deleted", body)
        self.assertIsInstance(body["deleted"], list)


if __name__ == "__main__":
    unittest.main()
