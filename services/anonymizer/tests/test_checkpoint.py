"""Tests for retry-from-checkpoint: checkpoint module, jobs store, and worker recovery."""
import json
import os
import tempfile

import pytest

from medanon_core.domain import Job, JobStatus
from pipeline.jobs.checkpoint import CHECKPOINT_INTERVAL, load_checkpoint, save_checkpoint
from pipeline.jobs import SqliteJobStore


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

class TestCheckpointHelpers:
    @pytest.fixture
    def job(self):
        return Job(id="job-cp-1", type="bulk-export", params={"server_url": "http://fhir"})

    @pytest.fixture
    def store(self, tmp_path):
        return SqliteJobStore(str(tmp_path / "jobs.db"))

    def test_load_checkpoint_none_by_default(self, job):
        assert load_checkpoint(job) is None

    def test_save_and_load_roundtrip(self, store, job):
        store.create.__func__  # ensure store exists
        created = store.create("bulk-export", {"server_url": "http://"})
        save_checkpoint(store, created, {"lines_written": 42})
        assert load_checkpoint(created) == {"lines_written": 42}

    def test_save_persists_to_store(self, store, job):
        created = store.create("bulk-export", {"server_url": "http://"})
        save_checkpoint(store, created, {"lines_written": 10})
        reloaded = store.get(created.id)
        assert reloaded.checkpoint_data == {"lines_written": 10}

    def test_save_swallows_store_error(self, job):
        """Checkpoint failure must not raise."""
        class _BrokenStore:
            def update_checkpoint(self, *a): raise RuntimeError("disk full")
        save_checkpoint(_BrokenStore(), job, {"lines_written": 5})
        # Job in-memory state still updated
        assert job.checkpoint_data == {"lines_written": 5}


# ---------------------------------------------------------------------------
# SqliteJobStore — checkpoint_data column
# ---------------------------------------------------------------------------

class TestJobStoreCheckpoint:
    @pytest.fixture
    def store(self, tmp_path):
        return SqliteJobStore(str(tmp_path / "jobs.db"))

    def test_create_job_has_no_checkpoint(self, store):
        job = store.create("cohort", {"server_url": "http://"})
        assert job.checkpoint_data is None

    def test_update_checkpoint_persists(self, store):
        job = store.create("bulk-export", {"server_url": "http://"})
        store.update_checkpoint(job.id, {"lines_written": 77})
        reloaded = store.get(job.id)
        assert reloaded.checkpoint_data == {"lines_written": 77}

    def test_full_update_preserves_checkpoint(self, store):
        job = store.create("bulk-export", {"server_url": "http://"})
        job.checkpoint_data = {"lines_written": 55}
        store.update(job)
        reloaded = store.get(job.id)
        assert reloaded.checkpoint_data == {"lines_written": 55}

    def test_checkpoint_survives_status_transition(self, store):
        job = store.create("bulk-export", {"server_url": "http://"})
        store.update_checkpoint(job.id, {"lines_written": 20})
        job2 = store.get(job.id)
        job2.status = JobStatus.RUNNING
        store.update(job2)
        final = store.get(job.id)
        assert final.checkpoint_data == {"lines_written": 20}


# ---------------------------------------------------------------------------
# Worker startup recovery
# ---------------------------------------------------------------------------

class TestWorkerStartupRecovery:
    @pytest.fixture
    def store(self, tmp_path):
        return SqliteJobStore(str(tmp_path / "jobs.db"))

    def test_running_jobs_reset_to_pending(self, store):
        job = store.create("bulk-export", {"server_url": "http://"})
        job.status = JobStatus.RUNNING
        store.update(job)

        import pipeline.jobs.worker as worker_mod
        original_store = worker_mod._store
        worker_mod._store = store
        try:
            recovered = worker_mod._recover_running_jobs()
        finally:
            worker_mod._store = original_store

        assert recovered == 1
        assert store.get(job.id).status == JobStatus.PENDING

    def test_pending_jobs_not_touched(self, store):
        store.create("bulk-export", {"server_url": "http://"})

        import pipeline.jobs.worker as worker_mod
        original_store = worker_mod._store
        worker_mod._store = store
        try:
            recovered = worker_mod._recover_running_jobs()
        finally:
            worker_mod._store = original_store

        assert recovered == 0

    def test_done_jobs_not_touched(self, store):
        job = store.create("bulk-export", {"server_url": "http://"})
        job.status = JobStatus.DONE
        job.result_path = "/output/result.ndjson"
        store.update(job)

        import pipeline.jobs.worker as worker_mod
        original_store = worker_mod._store
        worker_mod._store = store
        try:
            recovered = worker_mod._recover_running_jobs()
        finally:
            worker_mod._store = original_store

        assert recovered == 0
        assert store.get(job.id).status == JobStatus.DONE

    def test_multiple_running_jobs_all_recovered(self, store):
        for _ in range(3):
            job = store.create("bulk-export", {"server_url": "http://"})
            job.status = JobStatus.RUNNING
            store.update(job)

        import pipeline.jobs.worker as worker_mod
        original_store = worker_mod._store
        worker_mod._store = store
        try:
            recovered = worker_mod._recover_running_jobs()
        finally:
            worker_mod._store = original_store

        assert recovered == 3
        for j in store.list_jobs(status="pending"):
            assert j.status == JobStatus.PENDING

    def test_no_store_returns_zero(self):
        import pipeline.jobs.worker as worker_mod
        original_store = worker_mod._store
        worker_mod._store = None
        try:
            assert worker_mod._recover_running_jobs() == 0
        finally:
            worker_mod._store = original_store


# ---------------------------------------------------------------------------
# Checkpoint resume in executor (unit test without real FHIR server)
# ---------------------------------------------------------------------------

class TestBulkExportCheckpointResume:
    @pytest.fixture
    def store(self, tmp_path):
        return SqliteJobStore(str(tmp_path / "jobs.db"))

    def test_resumes_from_checkpoint_skips_written_lines(self, store, tmp_path, monkeypatch):
        """Executor with checkpoint{"lines_written": 2} skips first 2 resources."""
        import pipeline.jobs.worker as worker_mod

        output_dir = str(tmp_path / "out")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(worker_mod, "_OUTPUT_DIR", output_dir)
        monkeypatch.setattr(worker_mod, "_store", store)

        # Use resource_type param so the worker takes the simple single-type path
        job = store.create("bulk-export", {"server_url": "http://fhir", "resource_type": "Patient"})
        # Simulate 2 resources already written to output file
        output_path = os.path.join(output_dir, f"{job.id}.ndjson")
        with open(output_path, "w") as fh:
            fh.write('{"resourceType":"Patient","id":"p0"}\n')
            fh.write('{"resourceType":"Patient","id":"p1"}\n')
        store.update_checkpoint(job.id, {"lines_written": 2})
        job = store.get(job.id)  # reload with checkpoint

        resources = [
            {"resourceType": "Patient", "id": f"p{i}"}
            for i in range(5)
        ]

        written_ids: list = []

        def _fake_fetch_all(*a, **kw):
            for r in resources:
                yield (r["resourceType"], r)

        def _fake_process_batch(resources, settings, pseudonymizer=None):
            for resource in resources:
                written_ids.append(resource["id"])
            return resources

        try:
            import integrations.fhir.client as fhir_client
            import pipeline.processor as proc
            import pipeline.config.service as cs

            monkeypatch.setattr(fhir_client, "fetch_all_resource_types", _fake_fetch_all)
            monkeypatch.setattr(worker_mod, "process_data_batch", _fake_process_batch)
            monkeypatch.setattr(proc, "_get_default_pseudonymizer", lambda: None)
            monkeypatch.setattr(cs, "get_settings", lambda p: object())
        except Exception:
            pytest.skip("FHIR/processor not importable in this environment")

        worker_mod._execute_bulk_export(job)

        # Only resources p2, p3, p4 should be written (p0 and p1 skipped)
        assert written_ids == ["p2", "p3", "p4"]
        with open(output_path) as fh:
            lines = [l for l in fh.read().splitlines() if l]
        # 2 pre-existing + 3 new = 5 total
        assert len(lines) == 5
