"""Shared streaming infrastructure for bulk-operation executors.

Provides the producer-consumer pipeline, async checkpoint writer, and
chunk-processing helpers used by all export executors.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import time

from domain.jobs import JobStatus
from pipeline.jobs.checkpoint import save_checkpoint
from pipeline.manifest import strip_manifest_tag
from pipeline.processor import _BATCH_SIZE, process_data_batch
from utils.json_fast import dumps as _json_dumps, loads as _json_loads

_worker_log = logging.getLogger("medanon.worker")
_OUTPUT_DIR = os.environ.get("MEDANON_OUTPUT_DIR", "/output")
_PROGRESS_INTERVAL: int = int(os.environ.get("MEDANON_PROGRESS_INTERVAL", "500"))

_PIPELINE_QUEUE_SIZE: int = int(os.environ.get("MEDANON_PIPELINE_QUEUE_SIZE", "4"))
# Number of concurrent de-identification consumer threads per job.
# Default 1: optimal for single gPAS/NLP instances — additional consumers add
# thread overhead without faster service responses. Raise only when scaling
# gPAS/NLP horizontally (set to replica count). Keep width × MEDANON_PARALLEL_WORKERS
# within MEDANON_GLOBAL_MAX_THREADS (default 64).
_PIPELINE_WIDTH: int = int(os.environ.get("MEDANON_PIPELINE_WIDTH", "1"))
_PIPELINE_ENABLED: bool = os.environ.get(
    "MEDANON_PIPELINE_ENABLED", "true"
).strip().lower() in ("1", "true", "yes")
_COMPRESS_RESULTS: bool = os.environ.get(
    "MEDANON_COMPRESS_RESULTS", "false"
).strip().lower() in ("1", "true", "yes")
# The transformation manifest is released as a SEPARATE artifact (its own S3
# prefix + access control), never embedded per-resource. On by default; the
# streaming export writes a `<data>.manifest.ndjson` sidecar that publish_result
# delivers alongside the data + audit.
_MANIFEST_ARTIFACT_ENABLED: bool = os.environ.get(
    "MEDANON_MANIFEST_ARTIFACT_ENABLED", "true"
).strip().lower() in ("1", "true", "yes")


@contextlib.contextmanager
def manifest_sidecar(data_path: str):
    """Yield ``(manifest_path, manifest_fh)`` for the streaming export.

    Returns ``(None, None)`` when the manifest artifact is disabled. The sidecar
    is written next to *data_path*; ``publish_result`` delivers it under the
    ``manifests/`` prefix and removes the local file afterwards.
    """
    if not _MANIFEST_ARTIFACT_ENABLED:
        yield None, None
        return
    mpath = f"{data_path}.manifest.ndjson"
    with open(mpath, "w", encoding="utf-8") as mfh:
        yield mpath, mfh


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


def process_with_bisect_fallback(
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
            rid = (
                resource.get("id", "<no-id>")
                if isinstance(resource, dict)
                else "<no-id>"
            )
            _worker_log.error(
                "resource_processing_failed resource_type=%s id=%s error=%s",
                rtype,
                rid,
                exc,
                exc_info=True,
            )
            from pipeline.correction import quarantine_record

            return [
                (
                    quarantine_record(
                        error="processing error",
                        resource_type=rtype,
                        resource_id=rid,
                        stage="bisect",
                        error_type=type(exc).__name__,
                    ),
                    None,
                )
            ]

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
    except Exception as exc:
        _worker_log.debug(
            "batch_process_failed chunk_size=%d — bisecting: %s", len(chunk), exc
        )
        mid = len(chunk) // 2
        return process_with_bisect_fallback(
            chunk[:mid], settings, pseudonymizer, want_manifest
        ) + process_with_bisect_fallback(
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


class CheckpointWriter:
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


class _PipelineProgressDisplay:
    """Live terminal display of de-identification pipeline progress using Rich.

    Shows per-chunk stage timing and overall throughput in the worker terminal.
    Silently disabled when Rich is unavailable or when stdout is not a TTY
    (e.g. in CI / log-only environments).
    """

    _MAX_ROWS = 8  # keep the last N chunks in the display table

    def __init__(self, job_id: str, label: str) -> None:
        self._job_id = job_id
        self._label = label
        self._rows: list[
            dict
        ] = []  # {"chunk": int, "count": int, "duration": float, "status": str}
        self._total_resources = 0
        self._job_start = time.monotonic()
        self._live = None
        self._table = None
        self._lock = threading.Lock()

        try:
            from rich.live import Live
            from rich.table import Table
            from rich.console import Console
            import sys

            if not sys.stderr.isatty():
                return  # not a terminal — skip live display
            self._Console = Console
            self._Table = Table
            self._Live = Live
            self._enabled = True
        except ImportError:
            self._enabled = False

    def start(self) -> None:
        if not getattr(self, "_enabled", False):
            return
        self._table = self._build_table()
        self._live = self._Live(
            self._table,
            console=self._Console(stderr=True),
            refresh_per_second=2,
            transient=False,
        )
        self._live.start()

    def stop(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass

    def record_chunk(
        self, chunk_idx: int, resource_count: int, duration: float, ok: bool
    ) -> None:
        if not getattr(self, "_enabled", False):
            return
        with self._lock:
            self._total_resources += resource_count
            self._rows.append(
                {
                    "chunk": chunk_idx + 1,
                    "count": resource_count,
                    "duration": duration,
                    "status": "ok" if ok else "fallback",
                }
            )
            if len(self._rows) > self._MAX_ROWS:
                self._rows = self._rows[-self._MAX_ROWS :]
            elapsed = time.monotonic() - self._job_start
            throughput = self._total_resources / elapsed if elapsed > 0 else 0
            self._table = self._build_table(elapsed=elapsed, throughput=throughput)
            if self._live is not None:
                self._live.update(self._table)

    def _build_table(self, elapsed: float = 0.0, throughput: float = 0.0):
        from rich.table import Table
        from rich import box

        tbl = Table(
            title=f"[bold]MedAnon[/bold] De-identification Pipeline  "
            f"[dim]job={self._job_id[:16]}  label={self._label}[/dim]",
            box=box.SIMPLE_HEAD,
            show_footer=bool(self._rows),
            expand=False,
        )
        tbl.add_column("Chunk", justify="right", style="dim")
        tbl.add_column("Resources", justify="right")
        tbl.add_column("Duration", justify="right")
        tbl.add_column("Throughput", justify="right")
        tbl.add_column("", justify="center")

        for row in self._rows:
            tp = row["count"] / row["duration"] if row["duration"] > 0 else 0
            tbl.add_row(
                str(row["chunk"]),
                f"{row['count']:,}",
                f"{row['duration']:.1f}s",
                f"{tp:,.0f}/s",
                f"[green]{row['status']}[/green]"
                if row["status"] == "ok"
                else f"[yellow]{row['status']}[/yellow]",
            )

        if elapsed > 0:
            tbl.columns[0].footer = f"Total {int(elapsed)}s"
            tbl.columns[1].footer = f"{self._total_resources:,}"
            tbl.columns[3].footer = f"{throughput:,.0f}/s avg"

        return tbl


class DeidentificationPipeline:
    """FHIR fetch overlapped with concurrent de-identification processing.

    A single fetcher thread fills a bounded queue with resource chunks.
    MEDANON_PIPELINE_WIDTH consumer threads independently process chunks,
    each issuing its own NLP + gPAS HTTP calls — fanning out across all
    available replicas so a single job uses the full cluster capacity.

    Thread safety:
    - De-identification compute (NLP/gPAS HTTP) runs outside the output lock.
    - File writes, counters, and summary recording are serialized under
      self._output_lock so result ordering is deterministic within each write.
    - self._cancelled is a plain bool — GIL-protected reads/writes are safe in
      CPython without an explicit lock.
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
        manifest_fh=None,
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
        # When set, one manifest line per resource is streamed to this sidecar
        # handle (the transformation-manifest artifact, delivered separately).
        self._manifest_fh = manifest_fh
        self._cancelled = False
        self._chunk_queue: queue.Queue = queue.Queue(maxsize=_PIPELINE_QUEUE_SIZE)
        self._fetch_error: Exception | None = None
        self._consumer_errors: list[Exception] = []
        self._consumer_error_lock = threading.Lock()
        self._chunk_count: int = 0
        self._output_lock = threading.Lock()
        # Set by the fetcher when it has enqueued its last chunk (or failed).
        # Consumers use this to detect end-of-input without sentinel values.
        self._fetch_done = threading.Event()
        self._progress = _PipelineProgressDisplay(job.id, label)

    def run(self) -> tuple[int, bool]:
        """Run the pipeline and return ``(count, was_cancelled)``."""
        import gc

        _gc_orig = gc.get_threshold()
        # Reduce gen-2 GC frequency during bulk processing: the long-lived gPAS
        # and NLP caches are stable and don't benefit from frequent collection.
        gc.set_threshold(700, 50, 100)

        checkpoint_writer = CheckpointWriter(self._store, self._job)
        checkpoint_writer.start()
        self._progress.start()

        fetcher = threading.Thread(
            target=self._fetch_resources, daemon=True, name="medanon-fetcher"
        )
        fetcher.start()

        consumers = [
            threading.Thread(
                target=self._deidentification_worker,
                args=(checkpoint_writer,),
                daemon=True,
                name=f"medanon-consumer-{i}",
            )
            for i in range(_PIPELINE_WIDTH)
        ]
        for t in consumers:
            t.start()

        try:
            for t in consumers:
                t.join()
        finally:
            self._progress.stop()
            checkpoint_writer.stop()
            fetcher.join(timeout=5.0)
            gc.set_threshold(*_gc_orig)

        if self._fetch_error is not None:
            raise self._fetch_error
        if self._consumer_errors:
            raise self._consumer_errors[0]

        return self._count, self._cancelled

    def _fetch_resources(self) -> None:
        chunk: list[dict] = []
        try:
            for resource in self._gen:
                if self._cancelled:
                    break
                chunk.append(resource)
                if len(chunk) >= _BATCH_SIZE:
                    # Use a non-blocking put loop so a dead consumer (which stops
                    # draining the queue) doesn't block the fetcher thread forever.
                    while not self._cancelled:
                        try:
                            self._chunk_queue.put(chunk, timeout=1.0)
                            break
                        except queue.Full:
                            continue
                    chunk = []
            if chunk and not self._cancelled:
                while not self._cancelled:
                    try:
                        self._chunk_queue.put(chunk, timeout=1.0)
                        break
                    except queue.Full:
                        continue
        except Exception as exc:
            self._fetch_error = exc
        finally:
            # Signal consumers that no more chunks will be enqueued.
            # Consumers drain the queue after this event is set.
            self._fetch_done.set()

    def _deidentification_worker(self, checkpoint_writer: CheckpointWriter) -> None:
        while not self._cancelled:
            try:
                chunk = self._chunk_queue.get(timeout=1.0)
            except queue.Empty:
                # Exit once the fetcher is done AND the queue is drained.
                if self._fetch_done.is_set() and self._chunk_queue.empty():
                    break
                continue
            try:
                self._deidentify_chunk(chunk, checkpoint_writer)
            except Exception as exc:
                # Record the first consumer error and stop the pipeline.  The
                # fetcher will see _cancelled=True on its next iteration and stop
                # blocking on queue.put(), so the pipeline shuts down cleanly.
                _worker_log.error(
                    "consumer_crashed job=%s label=%s: %s",
                    self._job.id,
                    self._label,
                    exc,
                    exc_info=True,
                )
                with self._consumer_error_lock:
                    self._consumer_errors.append(exc)
                self._cancelled = True
                break

    def _deidentify_chunk(
        self, chunk: list[dict], checkpoint_writer: CheckpointWriter
    ) -> None:
        """Process one chunk: de-identify (no lock) then write results (locked)."""
        _want_manifest = self._summary is not None or self._manifest_fh is not None
        _chunk_start = time.monotonic()
        # Snapshot before processing: process_data_batch mutates dicts in-place
        # during finalize. If it raises mid-batch, process_with_bisect_fallback restores from
        # these snapshots to avoid re-sending already-pseudonymized values to gPAS.
        _snapshots = [_json_dumps(r) for r in chunk]

        # --- Compute phase: NLP + gPAS HTTP calls (runs outside output lock) ---
        _from_bisect = False
        try:
            batch_out = process_data_batch(
                chunk,
                self._settings,
                self._pseudonymizer,
                attach_manifest=True,
                _return_manifest=_want_manifest,
            )
            if _want_manifest:
                _results, _manifests = batch_out
            else:
                _results, _manifests = batch_out, None
        except Exception:
            # Binary-search fallback: isolates bad resources in log₂(N) depth;
            # good sub-chunks still benefit from batch gPAS de-duplication.
            fresh_chunk = [_json_loads(s) for s in _snapshots]
            pairs = process_with_bisect_fallback(
                fresh_chunk, self._settings, self._pseudonymizer, _want_manifest
            )
            _results = [r for r, _ in pairs]
            _manifests = [e for _, e in pairs]
            _from_bisect = True

        # --- Write phase: serialized under output lock across consumer threads ---
        with self._output_lock:
            chunk_idx = self._chunk_count
            self._chunk_count += 1
            for idx, result in enumerate(_results):
                _is_error = _from_bisect and result.get("error")
                entries = (
                    None if _is_error else (_manifests[idx] if _manifests else None)
                )
                if self._summary is not None:
                    if _is_error:
                        self._summary.record_error(
                            result.get("resourceType", "Unknown")
                        )
                    else:
                        self._summary.record_resource(result, manifest_entries=entries)
                if self._manifest_fh is not None:
                    if not _is_error:
                        write_manifest_line(self._manifest_fh, result, entries)
                    # Released data must not carry the manifest inline — it ships
                    # as the separate manifest artifact.
                    strip_manifest_tag(result)
                self._fh.write(_json_dumps(result) + "\n")
                self._count += 1
            self._fh.flush()
            if self._manifest_fh is not None:
                self._manifest_fh.flush()
            count_now = self._count

        # Checkpoint + cancellation check (outside lock — enqueue is thread-safe)
        if chunk_idx == 0 or count_now % _PROGRESS_INTERVAL < len(chunk):
            chk = {"phase": "processing", "lines_written": count_now}
            if self._cursor_state:
                chk["fhir_cursor"] = dict(self._cursor_state)
            checkpoint_writer.enqueue(chk)

        # Record chunk duration for the live progress display.
        self._progress.record_chunk(
            chunk_idx=chunk_idx,
            resource_count=len(chunk),
            duration=time.monotonic() - _chunk_start,
            ok=not _from_bisect,
        )

        if (chunk_idx + 1) % 5 == 0:
            fresh = self._store.get(self._job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                self._cancelled = (
                    True  # GIL-protected bool write; visible to all threads
                )


def write_manifest_line(manifest_fh, result: dict, entries) -> None:
    """Write one transformation-manifest line for *result* to the sidecar handle.

    Shape: ``{"resourceType", "id", "rules": [<manifest entries>]}`` — the entries
    already carry only rule name/match/action/path (no PHI values). Resources with
    no fired rule still get a line (empty ``rules``) so the manifest is a complete
    per-resource ledger of the released data.
    """
    manifest_fh.write(
        _json_dumps(
            {
                "resourceType": result.get("resourceType", "Unknown"),
                "id": result.get("id"),
                "rules": entries or [],
            }
        )
        + "\n"
    )


def stream_and_deidentify(
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
    manifest_fh=None,
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
        return DeidentificationPipeline(
            gen,
            settings,
            pseudonymizer,
            fh,
            start_count,
            store,
            job,
            label,
            summary,
            cursor_state=cursor_state,
            manifest_fh=manifest_fh,
        ).run()

    # Sequential fallback path (used when MEDANON_PIPELINE_ENABLED=false).
    count = start_count
    chunk: list[dict] = []
    _first_chunk = True

    _want_manifest = summary is not None or manifest_fh is not None

    def _flush() -> bool:
        nonlocal count, _first_chunk
        _snapshots = [_json_dumps(r) for r in chunk]
        try:
            out = process_data_batch(
                chunk,
                settings,
                pseudonymizer,
                attach_manifest=True,
                _return_manifest=_want_manifest,
            )
            results, manifests = out if _want_manifest else (out, None)
            for idx, result in enumerate(results):
                entries = manifests[idx] if manifests else None
                if summary is not None:
                    summary.record_resource(result, manifest_entries=entries)
                if manifest_fh is not None:
                    write_manifest_line(manifest_fh, result, entries)
                    strip_manifest_tag(result)
                fh.write(_json_dumps(result) + "\n")
                count += 1
        except Exception:
            fresh_chunk = [_json_loads(s) for s in _snapshots]
            # Binary-search fallback: isolates bad resources in log₂(N) depth.
            pairs = process_with_bisect_fallback(
                fresh_chunk, settings, pseudonymizer, want_manifest=_want_manifest
            )
            for result, entries in pairs:
                _is_error = bool(result.get("error"))
                if summary is not None:
                    if _is_error:
                        summary.record_error(result.get("resourceType", "Unknown"))
                    else:
                        summary.record_resource(result, manifest_entries=entries)
                if manifest_fh is not None:
                    if not _is_error:
                        write_manifest_line(manifest_fh, result, entries)
                    strip_manifest_tag(result)
                fh.write(_json_dumps(result) + "\n")
                count += 1

        fh.flush()
        if manifest_fh is not None:
            manifest_fh.flush()

        if _first_chunk or count % _PROGRESS_INTERVAL < len(chunk):
            chk = {"phase": "processing", "lines_written": count}
            if cursor_state:
                chk["fhir_cursor"] = dict(cursor_state)
            save_checkpoint(store, job, chk)
        _first_chunk = False

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


# Backward-compatible aliases.
process_stream_chunked = stream_and_deidentify
batch_or_bisect = process_with_bisect_fallback
AsyncCheckpointWriter = CheckpointWriter
PipelinedProcessor = DeidentificationPipeline
