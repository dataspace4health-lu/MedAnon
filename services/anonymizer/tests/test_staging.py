"""Tests for the staging layer: StagingStore, run_gpas_batch_for_batch, and staged_worker helpers.

These tests run locally (no Docker / PostgreSQL required) by mocking psycopg2.
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# Stub out psycopg2 so the tests run without the driver installed
# ---------------------------------------------------------------------------

def _make_psycopg2_stub():
    mod = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    pool_mod = types.ModuleType("psycopg2.pool")

    class RealDictCursor:
        pass

    def execute_values(cur, sql, args, template=None):
        cur._execute_values_called = True
        cur._execute_values_args = args
        cur.rowcount = len(args)

    extras.RealDictCursor = RealDictCursor
    extras.execute_values = execute_values

    class ThreadedConnectionPool:
        def __init__(self, minconn, maxconn, dsn):
            self._conn = MagicMock()
            self._conn.__enter__ = lambda s: s
            self._conn.__exit__ = MagicMock(return_value=False)

        def getconn(self):
            return self._conn

        def putconn(self, conn):
            pass

    pool_mod.ThreadedConnectionPool = ThreadedConnectionPool

    mod.extras = extras
    mod.pool = pool_mod
    sys.modules.setdefault("psycopg2", mod)
    sys.modules.setdefault("psycopg2.extras", extras)
    sys.modules.setdefault("psycopg2.pool", pool_mod)
    return mod


_make_psycopg2_stub()

from integrations.staging.store import StagingStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_resource(rtype: str, rid: str) -> dict:
    return {"resourceType": rtype, "id": rid, "name": f"{rtype}-{rid}"}


def _make_row(staging_id: int, resource: dict) -> dict:
    return {
        "id": staging_id,
        "resource_id": f"{resource['resourceType']}/{resource['id']}",
        "resource_type": resource["resourceType"],
        "resource_json": json.dumps(resource),
    }


# ---------------------------------------------------------------------------
# StagingStore tests
# ---------------------------------------------------------------------------

class TestStagingStore(unittest.TestCase):
    """Unit tests for StagingStore — all DB calls are mocked."""

    def setUp(self):
        self.store = StagingStore("postgresql://fake/fake", retention_days=7)
        self.store.ensure_schema()

    # ── ensure_schema ──────────────────────────────────────────────────────

    def test_ensure_schema_creates_pool(self):
        self.assertIsNotNone(self.store._pool)

    # ── stage_batch ────────────────────────────────────────────────────────

    def test_stage_batch_empty(self):
        result = self.store.stage_batch("job1", [])
        self.assertEqual(result, 0)

    def test_stage_batch_calls_execute_values(self):
        resources = [_make_resource("Patient", "p1"), _make_resource("Observation", "o1")]
        conn = self.store._pool.getconn()
        cur = MagicMock()
        cur.rowcount = 2
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)

        n = self.store.stage_batch("job1", resources)
        self.assertGreaterEqual(n, 0)  # rowcount comes from mock

    # ── mark_done / mark_error ─────────────────────────────────────────────

    def test_mark_done_empty(self):
        # Should not raise even with an empty list
        self.store.mark_done("job1", [])

    def test_mark_error_calls_update(self):
        conn = self.store._pool.getconn()
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)
        self.store.mark_error("job1", 42, "something went wrong")
        cur.execute.assert_called_once()
        args = cur.execute.call_args[0]
        self.assertIn("error", args[0])

    # ── count_by_status ────────────────────────────────────────────────────

    def test_count_by_status_returns_totals(self):
        conn = self.store._pool.getconn()
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [("pending", 10), ("done", 5), ("error", 1)]
        conn.cursor = MagicMock(return_value=cur)

        result = self.store.count_by_status("job1")
        self.assertEqual(result["pending"], 10)
        self.assertEqual(result["done"], 5)
        self.assertEqual(result["error"], 1)
        self.assertEqual(result["total"], 16)

    def test_count_by_status_missing_keys_default_zero(self):
        conn = self.store._pool.getconn()
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [("done", 3)]
        conn.cursor = MagicMock(return_value=cur)

        result = self.store.count_by_status("job1")
        self.assertEqual(result["pending"], 0)
        self.assertEqual(result["done"], 3)
        self.assertEqual(result["error"], 0)
        self.assertEqual(result["total"], 3)

    # ── reset_pending ──────────────────────────────────────────────────────

    def test_reset_pending_executes_update(self):
        conn = self.store._pool.getconn()
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.rowcount = 5
        conn.cursor = MagicMock(return_value=cur)

        n = self.store.reset_pending("job1")
        self.assertEqual(n, 5)
        cur.execute.assert_called_once()

    # ── cleanup_expired ────────────────────────────────────────────────────

    def test_cleanup_expired_executes_delete(self):
        conn = self.store._pool.getconn()
        cur = MagicMock()
        cur.__enter__ = lambda s: cur
        cur.__exit__ = MagicMock(return_value=False)
        cur.rowcount = 3
        conn.cursor = MagicMock(return_value=cur)

        n = self.store.cleanup_expired()
        self.assertEqual(n, 3)
        sql = cur.execute.call_args[0][0]
        self.assertIn("expires_at", sql)


# ---------------------------------------------------------------------------
# run_gpas_batch_for_batch tests
# ---------------------------------------------------------------------------

class TestRunGpasBatchForBatch(unittest.TestCase):

    def _make_batch_work(self, values: list[str]) -> list:
        """Build mock BatchWork items."""
        from pipeline.action_dispatcher import BatchWork
        works = []
        for i, v in enumerate(values):
            bw = MagicMock(spec=BatchWork)
            bw.element = {"value": v, "path": f"Patient.id[{i}]"}
            bw.params = {"domain": "test", "operation": "get-or-create"}
            works.append(bw)
        return works

    def test_no_gpas_params_returns_empty(self):
        from pipeline.gpas_orchestrator import run_gpas_batch_for_batch
        result = run_gpas_batch_for_batch([[]], "raise", MagicMock(), None)
        self.assertEqual(result, {})

    def test_empty_work_returns_empty(self):
        from pipeline.gpas_orchestrator import run_gpas_batch_for_batch
        params = {"domain": "test", "operation": "get-or-create"}
        result = run_gpas_batch_for_batch([[], []], "raise", MagicMock(), params)
        self.assertEqual(result, {})

    def test_single_batch_call_for_all_resources(self):
        from pipeline.gpas_orchestrator import run_gpas_batch_for_batch

        params = {"domain": "test", "operation": "get-or-create"}
        works_r1 = self._make_batch_work(["val-A", "val-B"])
        works_r2 = self._make_batch_work(["val-C", "val-A"])  # val-A is a duplicate

        pseudo = MagicMock()
        pseudo.pseudonymize_batch.return_value = {
            "val-A": "PSN-A", "val-B": "PSN-B", "val-C": "PSN-C",
        }

        result = run_gpas_batch_for_batch([works_r1, works_r2], "raise", pseudo, params)

        # Only one call to the pseudonymizer
        self.assertEqual(pseudo.pseudonymize_batch.call_count, 1)
        # Deduplicated — val-A appears only once
        called_values = pseudo.pseudonymize_batch.call_args[0][0]
        self.assertEqual(len(called_values), len(set(called_values)))
        self.assertIn("val-A", called_values)
        self.assertIn("val-B", called_values)
        self.assertIn("val-C", called_values)
        self.assertEqual(result["val-A"], "PSN-A")

    def test_gpas_unavailable_propagates(self):
        from pipeline.gpas_orchestrator import run_gpas_batch_for_batch
        from integrations.gpas.circuit_breaker import GpasUnavailableError

        params = {"domain": "test", "operation": "get-or-create"}
        works = self._make_batch_work(["val-X"])
        pseudo = MagicMock()
        pseudo.pseudonymize_batch.side_effect = GpasUnavailableError("circuit open")

        with self.assertRaises(GpasUnavailableError):
            run_gpas_batch_for_batch([works], "raise", pseudo, params)

    def test_skip_mode_swallows_generic_error(self):
        from pipeline.gpas_orchestrator import run_gpas_batch_for_batch

        params = {"domain": "test", "operation": "get-or-create"}
        works = self._make_batch_work(["val-X"])
        pseudo = MagicMock()
        pseudo.pseudonymize_batch.side_effect = RuntimeError("connection refused")

        result = run_gpas_batch_for_batch([works], "skip", pseudo, params)
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# FHIR client: fetch_resource_type with yield_cursors / start_url
# ---------------------------------------------------------------------------

class TestFetchResourceTypeExtensions(unittest.TestCase):

    def _bundle(self, resources, next_url=None):
        bundle = {
            "resourceType": "Bundle",
            "entry": [{"resource": r} for r in resources],
            "link": [],
        }
        if next_url:
            bundle["link"].append({"relation": "next", "url": next_url})
        return bundle

    def test_yield_cursors_includes_next_url(self):
        from integrations.fhir import reader as fhir_reader

        page1_resources = [_make_resource("Patient", "p1")]
        page2_resources = [_make_resource("Patient", "p2")]

        with patch.object(fhir_reader, "_get_json") as mock_get:
            mock_get.side_effect = [
                self._bundle(page1_resources, next_url="http://host/fhir/Patient?page=2"),
                self._bundle(page2_resources),
            ]
            results = list(fhir_reader.fetch_resource_type(
                "http://host/fhir", "Patient", yield_cursors=True
            ))

        self.assertEqual(len(results), 2)
        resource_p1, cursor_after_p1 = results[0]
        resource_p2, cursor_after_p2 = results[1]
        self.assertEqual(resource_p1["id"], "p1")
        self.assertEqual(cursor_after_p1, "http://host/fhir/Patient?page=2")
        self.assertIsNone(cursor_after_p2)

    def test_start_url_skips_initial_construction(self):
        from integrations.fhir import reader as fhir_reader

        with patch.object(fhir_reader, "_get_json") as mock_get:
            mock_get.return_value = self._bundle([_make_resource("Patient", "p99")])
            results = list(fhir_reader.fetch_resource_type(
                "http://host/fhir", "Patient",
                start_url="http://host/fhir/Patient?page=5",
            ))

        # First call should use the start_url directly
        called_url = mock_get.call_args_list[0][0][0]
        self.assertEqual(called_url, "http://host/fhir/Patient?page=5")
        self.assertEqual(results[0]["id"], "p99")

    def test_backward_compat_no_cursors(self):
        """Default (yield_cursors=False) yields plain resource dicts — no tuples."""
        from integrations.fhir import reader as fhir_reader

        with patch.object(fhir_reader, "_get_json") as mock_get:
            mock_get.return_value = self._bundle([_make_resource("Observation", "o1")])
            results = list(fhir_reader.fetch_resource_type("http://host/fhir", "Observation"))

        self.assertEqual(len(results), 1)
        # Must be a plain dict, not a tuple
        self.assertIsInstance(results[0], dict)
        self.assertEqual(results[0]["id"], "o1")


# ---------------------------------------------------------------------------
# Staged worker: _process_batch
# ---------------------------------------------------------------------------

class TestProcessBatch(unittest.TestCase):

    def _settings(self):
        s = MagicMock()
        s.rules = []
        s.filename = None
        s.processing_errors = "skip"
        s.rewrite_references = False
        s.rewrite_text_ids = False
        return s

    def test_process_batch_calls_gpas_once(self):
        from pipeline.jobs.staged_worker import _process_batch
        from pipeline.action_dispatcher import BatchWork

        resource = _make_resource("Patient", "p1")
        rows = [_make_row(1, resource)]
        settings = self._settings()

        pseudo = MagicMock()
        pseudo.pseudonymize_batch.return_value = {}

        with (
            patch("pipeline.processor._get_rules_for_resource", return_value=[]),
            patch("pipeline.processor.dispatch_pass1", return_value=[]),
            patch("pipeline.processor.run_gpas_batch_for_batch", return_value={}),
            patch("pipeline.processor.run_gpas_batch", return_value={}),
            patch("pipeline.processor._extract_gpas_params", return_value=None),
        ):
            results = _process_batch(rows, settings, pseudo, "skip")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "p1")

    def test_process_batch_handles_error_per_resource(self):
        """A per-resource error should emit an error sentinel, not abort the batch."""
        from pipeline.jobs.staged_worker import _process_batch

        resource = _make_resource("Condition", "c1")
        rows = [_make_row(1, resource)]
        settings = self._settings()
        pseudo = MagicMock()

        with (
            patch("pipeline.processor._get_rules_for_resource", return_value=[]),
            patch("pipeline.processor.dispatch_pass1", side_effect=RuntimeError("boom")),
            patch("pipeline.processor.run_gpas_batch_for_batch", return_value={}),
            patch("pipeline.processor._extract_gpas_params", return_value=None),
        ):
            results = _process_batch(rows, settings, pseudo, "skip")

        self.assertEqual(len(results), 1)
        self.assertIn("error", results[0])

    def test_process_batch_parses_string_json(self):
        """resource_json may be a JSON string (e.g. from NDJSON staging rows)."""
        from pipeline.jobs.staged_worker import _process_batch

        resource = _make_resource("Encounter", "e1")
        row = {
            "id": 1,
            "resource_id": "Encounter/e1",
            "resource_type": "Encounter",
            "resource_json": json.dumps(resource),  # string, not dict
        }
        settings = self._settings()
        pseudo = MagicMock()
        pseudo.pseudonymize_batch.return_value = {}

        with (
            patch("pipeline.processor._get_rules_for_resource", return_value=[]),
            patch("pipeline.processor.dispatch_pass1", return_value=[]),
            patch("pipeline.processor.run_gpas_batch_for_batch", return_value={}),
            patch("pipeline.processor.run_gpas_batch", return_value={}),
            patch("pipeline.processor._extract_gpas_params", return_value=None),
        ):
            results = _process_batch([row], settings, pseudo, "skip")

        self.assertEqual(results[0]["id"], "e1")


# ---------------------------------------------------------------------------
# JobService: submit_reprocess / get_staged_stats
# ---------------------------------------------------------------------------

class TestJobServiceStaging(unittest.TestCase):

    def _service_with_mock_store(self):
        from api.services.jobs import JobService
        from medanon_core.domain import Job, JobStatus

        service = JobService()
        mock_store = MagicMock()
        mock_job = MagicMock(spec=Job)
        mock_job.id = "job-abc"
        mock_job.type = "reprocess"
        mock_job.status = JobStatus.PENDING
        mock_job.created_at = "2024-01-01T00:00:00"
        mock_job.updated_at = "2024-01-01T00:00:00"
        mock_job.result_path = None
        mock_job.error = None
        mock_job.checkpoint_data = {}
        mock_store.get.return_value = mock_job
        mock_store.create.return_value = mock_job

        with patch("pipeline.jobs.store._job_store", mock_store):
            yield service, mock_store, mock_job

    def test_submit_reprocess_creates_job(self):
        from api.services.jobs import JobService
        from medanon_core.domain import Job, JobStatus

        service = JobService()
        mock_store = MagicMock()
        source_job = MagicMock()
        source_job.id = "src-job"
        new_job = MagicMock(spec=Job)
        new_job.id = "new-job"
        new_job.type = "reprocess"
        new_job.status = JobStatus.PENDING
        new_job.created_at = "2024-01-01T00:00:00"
        new_job.updated_at = "2024-01-01T00:00:00"
        new_job.result_path = None
        new_job.error = None
        new_job.checkpoint_data = {}

        mock_store.get.return_value = source_job  # source job exists
        mock_store.create.return_value = new_job

        with patch("pipeline.jobs.store._job_store", mock_store):
            result = service.submit_reprocess("src-job", config_profile="config_hipaa")

        mock_store.create.assert_called_once_with(
            "reprocess", {"source_job_id": "src-job", "config_profile": "config_hipaa"}
        )
        self.assertEqual(result["job_id"], "new-job")

    def test_get_staged_stats_no_staging(self):
        from api.services.jobs import JobService
        from medanon_core.domain import Job, JobStatus

        service = JobService()
        mock_store = MagicMock()
        job = MagicMock()
        mock_store.get.return_value = job

        with (
            patch("pipeline.jobs.store._job_store", mock_store),
            patch("pipeline.jobs.worker._staging", None),
        ):
            result = service.get_staged_stats("job-xyz")

        self.assertEqual(result["staging"], "unavailable")

    def test_get_staged_stats_with_staging(self):
        from api.services.jobs import JobService
        from medanon_core.domain import Job, JobStatus

        service = JobService()
        mock_store = MagicMock()
        mock_store.get.return_value = MagicMock()
        mock_staging = MagicMock()
        mock_staging.count_by_status.return_value = {
            "pending": 0, "done": 1000, "error": 2, "total": 1002
        }

        with (
            patch("pipeline.jobs.store._job_store", mock_store),
            patch("pipeline.jobs.worker._staging", mock_staging),
        ):
            result = service.get_staged_stats("job-xyz")

        self.assertEqual(result["done"], 1000)
        self.assertEqual(result["total"], 1002)


if __name__ == "__main__":
    unittest.main()
