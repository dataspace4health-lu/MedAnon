"""Integration tests for RedisJobStore.

These tests require a live Redis instance and are skipped automatically unless
MEDANON_TEST_REDIS_URL is set (e.g. redis://localhost:6379/1).

Use DB 1 (not DB 0) to avoid polluting the development database:
    export MEDANON_TEST_REDIS_URL=redis://localhost:6379/1
    cd services/anonymizer && python3 -m pytest tests/test_jobs_redis.py -v
"""

import os
import sys
import unittest

# typing.io shim for Python 3.13
import types
import typing  # noqa: E401
if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO
    _io_mod.TextIO = typing.TextIO
    _io_mod.BinaryIO = typing.BinaryIO
    sys.modules["typing.io"] = _io_mod

REDIS_URL = os.environ.get("MEDANON_TEST_REDIS_URL", "")


@unittest.skipUnless(REDIS_URL, "MEDANON_TEST_REDIS_URL not set — skipping Redis job store tests")
class TestRedisJobStore(unittest.TestCase):
    def setUp(self):
        from integrations.redis.job_store import RedisJobStore
        self.store = RedisJobStore(REDIS_URL)
        self.store._client.flushdb()

    def tearDown(self):
        self.store._client.flushdb()

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
        self.assertIsNone(self.store.get("nonexistent-id"))

    def test_update_status(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.RUNNING
        self.store.update(job)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.status, JobStatus.RUNNING)

    def test_update_result_path(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        job.status = JobStatus.DONE
        job.result_path = "/output/test.ndjson"
        self.store.update(job)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.result_path, "/output/test.ndjson")
        self.assertEqual(fetched.status, JobStatus.DONE)

    def test_update_error_field(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("cohort", {})
        job.status = JobStatus.ERROR
        job.error = "Connection refused"
        self.store.update(job)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.error, "Connection refused")

    def test_params_round_trip(self):
        params = {"server_url": "http://fhir/fhir", "level": "system", "count": 42}
        job = self.store.create("bulk-export", params)
        fetched = self.store.get(job.id)
        self.assertEqual(fetched.params, params)

    def test_list_all(self):
        self.store.create("bulk-export", {})
        self.store.create("cohort", {})
        jobs = self.store.list_jobs()
        self.assertEqual(len(jobs), 2)

    def test_list_empty(self):
        self.assertEqual(self.store.list_jobs(), [])

    def test_list_filter_by_status(self):
        from pipeline.jobs import JobStatus
        j1 = self.store.create("bulk-export", {})
        j1.status = JobStatus.DONE
        self.store.update(j1)
        self.store.create("cohort", {})
        pending = self.store.list_jobs(status="pending")
        done = self.store.list_jobs(status="done")
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(done), 1)

    def test_list_filter_by_type(self):
        self.store.create("bulk-export", {})
        self.store.create("cohort", {})
        exports = self.store.list_jobs(job_type="bulk-export")
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0].type, "bulk-export")

    def test_list_filter_combined(self):
        from pipeline.jobs import JobStatus
        j1 = self.store.create("bulk-export", {})
        j2 = self.store.create("bulk-export", {})
        j2.status = JobStatus.DONE
        self.store.update(j2)
        self.store.create("cohort", {})
        results = self.store.list_jobs(status="pending", job_type="bulk-export")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, j1.id)

    def test_list_pagination(self):
        for i in range(5):
            self.store.create("bulk-export", {"seq": i})
        page1 = self.store.list_jobs(limit=2, offset=0)
        page2 = self.store.list_jobs(limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        ids1 = {j.id for j in page1}
        ids2 = {j.id for j in page2}
        self.assertTrue(ids1.isdisjoint(ids2))

    def test_next_pending_returns_oldest(self):
        j1 = self.store.create("bulk-export", {"seq": 1})
        self.store.create("bulk-export", {"seq": 2})
        first = self.store.next_pending()
        self.assertEqual(first.id, j1.id)

    def test_next_pending_returns_none_when_empty(self):
        self.assertIsNone(self.store.next_pending())

    def test_notify_and_wait_for_job(self):
        job = self.store.create("bulk-export", {})
        self.store.notify_new_job(job.id)
        received_id = self.store.wait_for_job(timeout=2)
        self.assertEqual(received_id, job.id)

    def test_wait_for_job_timeout_returns_none(self):
        result = self.store.wait_for_job(timeout=1)
        self.assertIsNone(result)

    def test_status_index_updates_on_transition(self):
        from pipeline.jobs import JobStatus
        job = self.store.create("bulk-export", {})
        # Verify it starts in the pending set
        pending = self.store.list_jobs(status="pending")
        self.assertEqual(len(pending), 1)
        # Transition to running
        job.status = JobStatus.RUNNING
        self.store.update(job)
        self.assertEqual(len(self.store.list_jobs(status="pending")), 0)
        self.assertEqual(len(self.store.list_jobs(status="running")), 1)


if __name__ == "__main__":
    unittest.main()
