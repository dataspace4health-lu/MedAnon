"""Tests for Phase 2c: async job queue.

Covers:
- pipeline/jobs.py: JobStore CRUD, next_pending queue ordering, status transitions,
  list_jobs filtering/pagination, notify_new_job no-op
- api/routers/jobs.py: HTTP endpoints via FastAPI TestClient
  - POST /v1/jobs/bulk-export → 202
  - POST /v1/jobs/cohort → 202
  - GET  /v1/jobs              → 200 (list)
  - GET  /v1/jobs/{job_id} → 200 / 404
  - GET  /v1/jobs/{job_id}/result → 200 / 404 / 409 / 410
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Bootstrap: ensure the typing.io shim is loaded before fhirpathpy
# ---------------------------------------------------------------------------
import types, typing  # noqa: E401
if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO
    _io_mod.TextIO = typing.TextIO
    _io_mod.BinaryIO = typing.BinaryIO
    sys.modules["typing.io"] = _io_mod

os.environ["MEDANON_HASH_ALLOW_PLAIN"] = "true"


# ===========================================================================
# JobStore unit tests
# ===========================================================================

class TestJobStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        from pipeline.jobs import JobStore
        self.store = JobStore(db_path=self._tmp.name)

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_create_returns_pending_job(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {"server_url": "http://fhir/fhir"})
        self.assertIsNotNone(job.id)
        self.assertEqual(job.type, "bulk-export")
        self.assertEqual(job.status, JobStatus.PENDING)
        self.assertEqual(job.params["server_url"], "http://fhir/fhir")

    def test_get_returns_job(self):
        job = self.store.create("cohort", {"search_type": "Patient"})
        fetched = self.store.get(job.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, job.id)
        self.assertEqual(fetched.type, "cohort")

    def test_get_returns_none_for_unknown_id(self):
        result = self.store.get("nonexistent-id")
        self.assertIsNone(result)

    def test_update_status(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.RUNNING
        self.store.update(job)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, JobStatus.RUNNING)

    def test_update_result_path_and_error(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.DONE
        job.result_path = "/output/job_abc.ndjson"
        self.store.update(job)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.result_path, "/output/job_abc.ndjson")
        self.assertEqual(fetched.status, JobStatus.DONE)

    def test_update_error_field(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.ERROR
        job.error = "Connection refused"
        self.store.update(job)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.error, "Connection refused")
        self.assertEqual(fetched.status, JobStatus.ERROR)

    def test_next_pending_returns_oldest(self):
        from pipeline.jobs import JobStatus
        j1 = self.store.create("bulk-export", {"seq": 1})
        j2 = self.store.create("bulk-export", {"seq": 2})
        first = self.store.next_pending()
        self.assertEqual(first.id, j1.id)

    def test_next_pending_returns_none_when_empty(self):
        result = self.store.next_pending()
        self.assertIsNone(result)

    def test_next_pending_skips_non_pending(self):
        from pipeline.jobs import JobStatus
        j1 = self.store.create("bulk-export", {})
        j1.status = JobStatus.RUNNING
        self.store.update(j1)
        j2 = self.store.create("cohort", {})
        next_job = self.store.next_pending()
        self.assertEqual(next_job.id, j2.id)

    def test_params_round_trip(self):
        params = {"server_url": "http://fhir/fhir", "level": "system", "count": 42}
        job = self.store.create("bulk-export", params)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.params, params)


# ===========================================================================
# SqliteJobStore list_jobs + notify_new_job tests
# ===========================================================================

class TestJobStoreListJobs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        from pipeline.jobs import JobStore
        self.store = JobStore(db_path=self._tmp.name)

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_list_all_returns_all_jobs(self):
        self.store.create("bulk-export", {"seq": 1})
        self.store.create("cohort", {"seq": 2})
        jobs = self.store.list_jobs()
        self.assertEqual(len(jobs), 2)

    def test_list_empty(self):
        self.assertEqual(self.store.list_jobs(), [])

    def test_list_filter_by_status(self):
        from pipeline.jobs import JobStatus
        j1 = self.store.create("bulk-export", {})
        j1.status = JobStatus.RUNNING
        self.store.update(j1)
        self.store.create("cohort", {})
        pending = self.store.list_jobs(status="pending")
        running = self.store.list_jobs(status="running")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].status, JobStatus.PENDING)
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].status, JobStatus.RUNNING)

    def test_list_filter_by_type(self):
        self.store.create("bulk-export", {})
        self.store.create("cohort", {})
        exports = self.store.list_jobs(job_type="bulk-export")
        cohorts = self.store.list_jobs(job_type="cohort")
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].type, "bulk-export")
        self.assertEqual(len(cohorts), 1)

    def test_list_filter_combined_status_and_type(self):
        from pipeline.jobs import JobStatus
        j1 = self.store.create("bulk-export", {})
        self.store.create("cohort", {})         # pending cohort — should not match
        j3 = self.store.create("bulk-export", {})
        j3.status = JobStatus.DONE
        self.store.update(j3)
        results = self.store.list_jobs(status="pending", job_type="bulk-export")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, j1.id)

    def test_list_ordered_newest_first(self):
        j1 = self.store.create("bulk-export", {"seq": 1})
        j2 = self.store.create("bulk-export", {"seq": 2})
        jobs = self.store.list_jobs()
        # Most recent first
        self.assertEqual(jobs[0].id, j2.id)
        self.assertEqual(jobs[1].id, j1.id)

    def test_list_pagination(self):
        for i in range(5):
            self.store.create("bulk-export", {"seq": i})
        page1 = self.store.list_jobs(limit=2, offset=0)
        page2 = self.store.list_jobs(limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        # Pages must not overlap
        ids1 = {j.id for j in page1}
        ids2 = {j.id for j in page2}
        self.assertTrue(ids1.isdisjoint(ids2))

    def test_notify_new_job_is_noop(self):
        """SqliteJobStore.notify_new_job must not raise."""
        job = self.store.create("bulk-export", {})
        self.store.notify_new_job(job.id)  # should not raise

    def test_cancel_pending_job(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        result = self.store.cancel(job.id)
        self.assertTrue(result)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, JobStatus.CANCELLED)

    def test_cancel_running_job(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.RUNNING
        self.store.update(job)
        result = self.store.cancel(job.id)
        self.assertTrue(result)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, JobStatus.CANCELLED)

    def test_cancel_done_job_returns_false(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.DONE
        self.store.update(job)
        result = self.store.cancel(job.id)
        self.assertFalse(result)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, JobStatus.DONE)


# ===========================================================================
# Jobs router HTTP tests
# ===========================================================================

_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


def _make_client_with_store(store):
    """Return a TestClient with the given JobStore injected as the module singleton."""
    import pipeline.jobs.store as _jobs_store_mod
    _jobs_store_mod._job_store = store

    with patch.dict(os.environ, {
        "MEDANON_CONFIG_DIR": _CONFIG_DIR,
        "MEDANON_API_KEY": "",
        "MEDANON_RATE_LIMIT_ENABLED": "false",
        "MEDANON_CORS_ORIGINS": "",
        "MEDANON_HASH_ALLOW_PLAIN": "true",
        "GPAS_URL": "",
        "FHIR_SOURCE_URL": "http://fhir:8080/fhir",
        "LOG_LEVEL": "WARNING",
    }, clear=False):
        for mod in ("api.auth", "api.main"):
            if mod in sys.modules:
                del sys.modules[mod]
        from api.main import app
        from pipeline.config.service import clear_settings_cache
        clear_settings_cache()
        from fastapi.testclient import TestClient
        return TestClient(app, raise_server_exceptions=False)


class TestJobsRouter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        from pipeline.jobs import JobStore
        self.store = JobStore(db_path=self._tmp.name)
        self.client = _make_client_with_store(self.store)

    def tearDown(self):
        import pipeline.jobs.store as _jobs_store_mod
        _jobs_store_mod._job_store = None
        os.unlink(self._tmp.name)

    # ── POST /v1/jobs/bulk-export ────────────────────────────────────────────

    def test_submit_bulk_export_returns_202(self):
        resp = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "http://fhir:8080/fhir"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("job_id", body)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["type"], "bulk-export")

    def test_submit_bulk_export_uses_env_url(self):
        """server_url may be omitted when FHIR_SOURCE_URL env var is set."""
        # FHIR_SOURCE_URL must be set at request time (os.environ is read by _resolve_url)
        with patch.dict(os.environ, {"FHIR_SOURCE_URL": "http://fhir:8080/fhir"}):
            resp = self.client.post("/v1/jobs/bulk-export", json={})
        self.assertEqual(resp.status_code, 202)

    def test_submit_bulk_export_422_without_url(self):
        """Should return 422 when server_url is absent AND env var is unset."""
        with patch.dict(os.environ, {"FHIR_SOURCE_URL": ""}, clear=False):
            resp = self.client.post("/v1/jobs/bulk-export", json={})
        self.assertIn(resp.status_code, (422, 503))

    # ── POST /v1/jobs/cohort ────────────────────────────────────────────────

    def test_submit_cohort_returns_202(self):
        resp = self.client.post(
            "/v1/jobs/cohort",
            json={"server_url": "http://fhir:8080/fhir", "search_type": "Patient"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["type"], "cohort")
        self.assertEqual(body["status"], "pending")

    # ── GET /v1/jobs/{job_id} ───────────────────────────────────────────────

    def test_get_job_status_200(self):
        resp_post = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "http://fhir:8080/fhir"},
        )
        job_id = resp_post.json()["job_id"]
        resp = self.client.get(f"/v1/jobs/{job_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertIn("status", body)

    def test_get_job_status_404(self):
        resp = self.client.get("/v1/jobs/nonexistent-job-id")
        self.assertEqual(resp.status_code, 404)

    # ── GET /v1/jobs/{job_id}/result ────────────────────────────────────────

    def test_get_result_404_unknown_job(self):
        resp = self.client.get("/v1/jobs/unknown-id/result")
        self.assertEqual(resp.status_code, 404)

    def test_get_result_409_job_not_done(self):
        resp_post = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "http://fhir:8080/fhir"},
        )
        job_id = resp_post.json()["job_id"]
        resp = self.client.get(f"/v1/jobs/{job_id}/result")
        self.assertEqual(resp.status_code, 409)

    def test_get_result_410_when_file_missing(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.DONE
        job.result_path = "/nonexistent/path/result.ndjson"
        self.store.update(job)
        resp = self.client.get(f"/v1/jobs/{job.id}/result")
        self.assertEqual(resp.status_code, 410)

    def test_get_result_200_streams_ndjson(self):
        from pipeline.jobs import JobStatus
        with tempfile.NamedTemporaryFile(
            suffix=".ndjson", delete=False, mode="w"
        ) as f:
            f.write('{"resourceType":"Patient","id":"p1"}\n')
            result_path = f.name
        try:
            job = self.store.create("bulk-export", {})
            job.status = JobStatus.DONE
            job.result_path = result_path
            self.store.update(job)
            resp = self.client.get(f"/v1/jobs/{job.id}/result")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("ndjson", resp.headers.get("content-type", ""))
        finally:
            os.unlink(result_path)

    def test_job_store_503_when_not_initialized(self):
        import pipeline.jobs.store as _jobs_store_mod
        _jobs_store_mod._job_store = None
        resp = self.client.get("/v1/jobs/any-id")
        self.assertEqual(resp.status_code, 503)

    # ── GET /v1/jobs ────────────────────────────────────────────────────────

    def test_list_jobs_empty(self):
        resp = self.client.get("/v1/jobs")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_list_jobs_200_returns_submitted_jobs(self):
        self.client.post("/v1/jobs/bulk-export", json={"server_url": "http://fhir:8080/fhir"})
        self.client.post("/v1/jobs/cohort", json={"server_url": "http://fhir:8080/fhir", "search_type": "Patient"})
        resp = self.client.get("/v1/jobs")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 2)
        types = {j["type"] for j in body}
        self.assertIn("bulk-export", types)
        self.assertIn("cohort", types)

    def test_list_jobs_filter_by_status(self):
        self.client.post("/v1/jobs/bulk-export", json={"server_url": "http://fhir:8080/fhir"})
        resp = self.client.get("/v1/jobs?status=pending")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["status"], "pending")

    def test_list_jobs_filter_by_type(self):
        self.client.post("/v1/jobs/bulk-export", json={"server_url": "http://fhir:8080/fhir"})
        self.client.post("/v1/jobs/cohort", json={"server_url": "http://fhir:8080/fhir", "search_type": "Patient"})
        resp = self.client.get("/v1/jobs?type=bulk-export")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["type"], "bulk-export")

    def test_list_jobs_503_when_not_initialized(self):
        import pipeline.jobs.store as _jobs_store_mod
        _jobs_store_mod._job_store = None
        resp = self.client.get("/v1/jobs")
        self.assertEqual(resp.status_code, 503)

    def test_list_jobs_pagination(self):
        for _ in range(5):
            self.client.post("/v1/jobs/bulk-export", json={"server_url": "http://fhir:8080/fhir"})
        page1 = self.client.get("/v1/jobs?limit=2&offset=0").json()
        page2 = self.client.get("/v1/jobs?limit=2&offset=2").json()
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        ids1 = {j["job_id"] for j in page1}
        ids2 = {j["job_id"] for j in page2}
        self.assertTrue(ids1.isdisjoint(ids2))

    # ── SSRF protection ────────────────────────────────────────────────────

    def test_submit_bulk_export_rejects_private_ip(self):
        resp = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "http://169.254.169.254/latest/meta-data/"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_submit_bulk_export_rejects_file_scheme(self):
        resp = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "file:///etc/passwd"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_submit_bulk_export_rejects_loopback(self):
        resp = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "http://127.0.0.1:8080/fhir"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_submit_cohort_rejects_private_ip(self):
        resp = self.client.post(
            "/v1/jobs/cohort",
            json={
                "server_url": "http://169.254.169.254/latest/meta-data/",
                "search_type": "Patient",
            },
        )
        self.assertEqual(resp.status_code, 422)

    # ── DELETE /v1/jobs/{job_id} ───────────────────────────────────────────

    def test_cancel_pending_job_200(self):
        resp_post = self.client.post(
            "/v1/jobs/bulk-export",
            json={"server_url": "http://fhir:8080/fhir"},
        )
        job_id = resp_post.json()["job_id"]
        resp = self.client.delete(f"/v1/jobs/{job_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["job_id"], job_id)
        self.assertEqual(body["status"], "cancelled")

    def test_cancel_nonexistent_job_404(self):
        resp = self.client.delete("/v1/jobs/nonexistent-id")
        self.assertEqual(resp.status_code, 404)

    def test_cancel_done_job_returns_done(self):
        """Cancelling a completed job should return its current state (no error)."""
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.DONE
        job.result_path = "/output/test.ndjson"
        self.store.update(job)
        resp = self.client.delete(f"/v1/jobs/{job.id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "done")

    def test_cancel_503_when_store_not_initialized(self):
        import pipeline.jobs.store as _jobs_store_mod
        _jobs_store_mod._job_store = None
        resp = self.client.delete("/v1/jobs/any-id")
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
