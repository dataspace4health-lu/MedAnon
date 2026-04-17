"""Shared streaming infrastructure for bulk-operation executors.

Provides the producer-consumer pipeline, async checkpoint writer, and
chunk-processing helpers used by all export executors.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from pathlib import Path

from medanon_core.domain import JobStatus
from pipeline.jobs.checkpoint import save_checkpoint
from pipeline.processor import _BATCH_SIZE, _CappedSet, process_data_batch
from utils.json_fast import dumps as _json_dumps

_worker_log = logging.getLogger("medanon.worker")
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_PROGRESS_INTERVAL: int = int(os.environ.get("MEDANON_PROGRESS_INTERVAL", "500"))

_PIPELINE_QUEUE_SIZE: int = int(os.environ.get("MEDANON_PIPELINE_QUEUE_SIZE", "4"))
_PIPELINE_ENABLED: bool = os.environ.get(
    "MEDANON_PIPELINE_ENABLED", "true"
).strip().lower() in ("1", "true", "yes")
_COMPRESS_RESULTS: bool = os.environ.get(
    "MEDANON_COMPRESS_RESULTS", "false"
).strip().lower() in ("1", "true", "yes")

# FHIR infrastructure resource types excluded from bulk export.
INFRA_RESOURCE_TYPES = frozenset(
    {
        "CapabilityStatement",
        "OperationDefinition",
        "SearchParameter",
        "StructureDefinition",
        "CompartmentDefinition",
        "ImplementationGuide",
        "CodeSystem",
        "ValueSet",
        "ConceptMap",
        "NamingSystem",
        "OperationOutcome",
        "Bundle",
    }
)


def compress_ndjson(path: str) -> str:
    """Gzip-compress *path* in place.  Returns the ``.ndjson.gz`` path."""
    import gzip
    import shutil

    gz_path = path + ".gz"
    with open(path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out, length=1 << 20)
    os.remove(path)
    _worker_log.info("ndjson_compressed src=%s gz=%s", path, gz_path)
    return gz_path


def batch_or_bisect(
    chunk: list[dict],
    settings,
    pseudonymizer,
    want_manifest: bool,
) -> list[tuple[dict, list[dict] | None]]:
    """Try to batch-process *chunk*; bisect on failure to isolate bad resources.

    On batch failure, splits the chunk in half and recurses rather than falling
    back to N=1 per-resource calls.  Good sub-chunks still benefit from batch
    gPAS de-duplication; worst-case depth is log₂(N).

    Returns a list of ``(result_dict | error_dict, manifest_entries | None)``
    pairs in the same order as *chunk*.
    """
    if not chunk:
        return []

    if len(chunk) == 1:
        resource = chunk[0]
        try:
            out = process_data_batch(
                [resource],
                settings,
                pseudonymizer,
                attach_manifest=True,
                _return_manifest=want_manifest,
            )
            if want_manifest:
                return [(out[0][0], out[1][0] if out[1] else None)]
            return [(out[0], None)]
        except Exception as exc:
            rtype = (
                resource.get("resourceType", "Unknown")
                if isinstance(resource, dict)
                else "Unknown"
            )
            _worker_log.error("fallback resource_type=%s error=%s", rtype, exc)
            return [({"error": "processing error", "resourceType": rtype}, None)]

    try:
        out = process_data_batch(
            chunk,
            settings,
            pseudonymizer,
            attach_manifest=True,
            _return_manifest=want_manifest,
        )
        if want_manifest:
            results, manifests = out
            return list(zip(results, manifests if manifests else [None] * len(results)))
        return [(r, None) for r in out]
    except Exception:
        mid = len(chunk) // 2
        return batch_or_bisect(
            chunk[:mid], settings, pseudonymizer, want_manifest
        ) + batch_or_bisect(
            chunk[mid:], settings, pseudonymizer, want_manifest
        )


def skip_to(gen, n: int):
    """Yield items from *gen*, skipping the first *n* entries (resume helper)."""
    for i, item in enumerate(gen):
        if i >= n:
            yield item


def cursor_tracking_gen(cursor_aware_gen, cursor_out: dict):
    """Strip cursor info from a yield_cursors=True generator and update cursor_out.

    Wraps a ``fetch_all_resource_types(yield_cursors=True)`` generator that yields
    ``(rt, resource, page_url, page_offset)`` 4-tuples.  Yields plain ``resource``
    dicts and keeps *cursor_out* current so checkpoint writers can include the
    exact FHIR pagination position.

    On resume, the executor reads *cursor_out* from the checkpoint and passes
    ``completed_rts``, ``current_rt``, and ``current_rt_start_url`` to
    ``fetch_all_resource_types``, skipping only ``page_offset`` resources within
    the current page rather than re-downloading all prior FHIR pages.
    """
    completed: list[str] = []
    current_rt: str | None = None
    for rt, resource, page_url, page_offset in cursor_aware_gen:
        if rt != current_rt:
            if current_rt is not None:
                completed.append(current_rt)
            current_rt = rt
        cursor_out["completed_rts"] = list(completed)
        cursor_out["current_rt"] = rt
        cursor_out["page_url"] = page_url
        cursor_out["page_offset"] = page_offset
        yield resource


class AsyncCheckpointWriter:
    """Background thread that batches and writes checkpoints.

    Checkpoint writes are queued and flushed periodically, so the main
    processing thread is never blocked waiting for database I/O.
    """

    def __init__(self, store, job):
        self._store = store
        self._job = job
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._last_data: dict | None = None

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, data: dict) -> None:
        """Queue checkpoint data for async write (non-blocking)."""
        self._queue.put(data)

    def stop(self) -> None:
        """Stop the writer and flush any pending checkpoint."""
        self._stop.set()
        self._queue.put(None)  # wake up the thread
        self._thread.join(timeout=5.0)
        if self._last_data is not None:
            save_checkpoint(self._store, self._job, self._last_data)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._queue.get(timeout=1.0)
                if data is None:
                    break
                # Drain to get the latest checkpoint (coalesce writes).
                while True:
                    try:
                        newer = self._queue.get_nowait()
                        if newer is None:
                            break
                        data = newer
                    except queue.Empty:
                        break
                self._last_data = data
                save_checkpoint(self._store, self._job, data)
            except queue.Empty:
                continue
            except Exception as exc:
                _worker_log.warning(
                    "async_checkpoint_error job=%s: %s", self._job.id, exc
                )


class PipelinedProcessor:
    """Overlaps FHIR fetch with de-identification processing.

    The fetcher thread fills a bounded queue with chunks of resources.
    The main thread consumes chunks and processes them, hiding fetch
    latency behind processing time.
    """

    def __init__(
        self,
        gen,
        settings,
        pseudonymizer,
        fh,
        start_count: int,
        store,
        job,
        label: str,
        summary=None,
        cursor_state=None,
    ):
        self._gen = gen
        self._settings = settings
        self._pseudonymizer = pseudonymizer
        self._fh = fh
        self._count = start_count
        self._store = store
        self._job = job
        self._label = label
        self._summary = summary
        self._cursor_state = cursor_state
        self._cancelled = False
        self._chunk_queue: queue.Queue = queue.Queue(maxsize=_PIPELINE_QUEUE_SIZE)
        self._fetch_error: Exception | None = None
        self._seen_values = _CappedSet()
        self._chunk_count: int = 0

    def run(self) -> tuple[int, bool]:
        """Run the pipeline and return ``(count, was_cancelled)``."""
        import gc
        _gc_orig = gc.get_threshold()
        # Reduce gen-2 GC frequency during bulk processing: the long-lived gPAS
        # and NLP caches are stable and don't benefit from frequent collection.
        gc.set_threshold(700, 50, 100)

        checkpoint_writer = AsyncCheckpointWriter(self._store, self._job)
        checkpoint_writer.start()

        fetcher = threading.Thread(target=self._fetcher_loop, daemon=True)
        fetcher.start()

        try:
            while True:
                try:
                    item = self._chunk_queue.get(timeout=2.0)
                except queue.Empty:
                    if not fetcher.is_alive():
                        break
                    continue

                if item is None:  # sentinel: fetcher done
                    break

                cancelled = self._process_chunk(item, checkpoint_writer)
                if cancelled:
                    self._cancelled = True
                    break
        finally:
            checkpoint_writer.stop()
            fetcher.join(timeout=5.0)
            gc.set_threshold(*_gc_orig)

        if self._fetch_error is not None:
            raise self._fetch_error

        return self._count, self._cancelled

    def _fetcher_loop(self) -> None:
        chunk: list[dict] = []
        try:
            for resource in self._gen:
                chunk.append(resource)
                if len(chunk) >= _BATCH_SIZE:
                    self._chunk_queue.put(chunk)
                    chunk = []
                    if self._cancelled:
                        break
            if chunk and not self._cancelled:
                self._chunk_queue.put(chunk)
        except Exception as exc:
            self._fetch_error = exc
        finally:
            self._chunk_queue.put(None)  # sentinel

    def _process_chunk(self, chunk: list[dict], checkpoint_writer: AsyncCheckpointWriter) -> bool:
        """Process one chunk.  Returns True if the job was cancelled."""
        _want_manifest = self._summary is not None
        try:
            batch_out = process_data_batch(
                chunk,
                self._settings,
                self._pseudonymizer,
                attach_manifest=True,
                _exclude_cached=self._seen_values,
                _seen_accumulator=self._seen_values,
                _return_manifest=_want_manifest,
            )
            if _want_manifest:
                results, manifest_list = batch_out
            else:
                results = batch_out
                manifest_list = None
            for idx, result in enumerate(results):
                self._fh.write(_json_dumps(result) + "\n")
                self._count += 1
                if self._summary is not None:
                    entries = manifest_list[idx] if manifest_list else None
                    self._summary.record_resource(result, manifest_entries=entries)
        except Exception:
            # Binary-search fallback: isolates bad resources in log₂(N) depth;
            # good sub-chunks still benefit from batch gPAS de-duplication.
            pairs = batch_or_bisect(
                chunk, self._settings, self._pseudonymizer, _want_manifest
            )
            for result, fb_entries in pairs:
                self._fh.write(_json_dumps(result) + "\n")
                if self._summary is not None:
                    if result.get("error"):
                        self._summary.record_error(result.get("resourceType", "Unknown"))
                    else:
                        self._summary.record_resource(result, manifest_entries=fb_entries)
                self._count += 1

        self._fh.flush()

        if self._count % _PROGRESS_INTERVAL < len(chunk):
            chk = {"phase": "processing", "lines_written": self._count}
            if self._cursor_state:
                chk["fhir_cursor"] = dict(self._cursor_state)
            checkpoint_writer.enqueue(chk)

        self._chunk_count += 1
        if self._chunk_count % 5 == 0:
            fresh = self._store.get(self._job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                return True
        return False


def process_stream_chunked(
    gen,
    settings,
    pseudonymizer,
    fh,
    start_count: int,
    store,
    job,
    label: str,
    summary=None,
    cursor_state=None,
) -> tuple[int, bool]:
    """Buffer resources from *gen* into chunks and process each via
    :func:`~pipeline.processor.process_data_batch`, writing results to *fh*.

    Returns ``(count, was_cancelled)`` where *count* is the total lines written
    (including *start_count*).

    When *summary* is provided, each processed resource is recorded for the
    completion summary.

    When ``MEDANON_PIPELINE_ENABLED=true`` (default), uses a producer-consumer
    pattern to overlap FHIR fetch with de-identification processing.

    When *cursor_state* is provided (dict), it is included in each checkpoint
    so that on crash recovery the FHIR pagination position is restored.
    """
    if _PIPELINE_ENABLED:
        return PipelinedProcessor(
            gen, settings, pseudonymizer, fh, start_count, store, job, label, summary,
            cursor_state=cursor_state,
        ).run()

    # Sequential fallback path (used when MEDANON_PIPELINE_ENABLED=false).
    count = start_count
    chunk: list[dict] = []
    seen_values = _CappedSet()

    def _flush() -> bool:
        nonlocal count
        try:
            results = process_data_batch(
                chunk, settings, pseudonymizer, attach_manifest=True,
                _exclude_cached=seen_values,
                _seen_accumulator=seen_values,
            )
            for result in results:
                fh.write(_json_dumps(result) + "\n")
                count += 1
                if summary is not None:
                    summary.record_resource(result)
        except Exception:
            # Binary-search fallback: isolates bad resources in log₂(N) depth.
            pairs = batch_or_bisect(chunk, settings, pseudonymizer, want_manifest=False)
            for result, _ in pairs:
                fh.write(_json_dumps(result) + "\n")
                if summary is not None:
                    if result.get("error"):
                        summary.record_error(result.get("resourceType", "Unknown"))
                    else:
                        summary.record_resource(result)
                count += 1

        fh.flush()

        if count % _PROGRESS_INTERVAL < len(chunk):
            chk = {"phase": "processing", "lines_written": count}
            if cursor_state:
                chk["fhir_cursor"] = dict(cursor_state)
            save_checkpoint(store, job, chk)

        fresh = store.get(job.id)
        if fresh and fresh.status == JobStatus.CANCELLED:
            return True
        return False

    for resource in gen:
        chunk.append(resource)
        if len(chunk) >= _BATCH_SIZE:
            if _flush():
                _worker_log.info("%s_cancelled job=%s at_line=%d", label, job.id, count)
                return count, True
            chunk = []

    if chunk and _flush():
        return count, True

    return count, False
