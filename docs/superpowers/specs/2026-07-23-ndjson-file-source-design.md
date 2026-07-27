# NDJSON File Source (Object Storage / Local Filesystem) — Design

**Date:** 2026-07-23
**Status:** Approved, ready for implementation planning

## Problem

MedAnon can only ingest from a live FHIR server. To de-identify an existing bulk
export we must first load it into HAPI, which is prohibitively expensive:

- Measured cost: **23.9 KB of hapi-db per resource** (3,055 MB for 130,772
  resources) — roughly **19x** the NDJSON on disk, because HAPI writes search
  index rows for every resource.
- The 1000-patient dataset at
  `/data/fhir-datasets/sample-bulk-fhir-datasets-1000-patients/` is
  **1,441,881 resources / 1,132 patients / 1.6 GB of NDJSON**. Loading it needs
  **~33 GB** of database against **9.5 GB** available. It does not fit.

Separately, the staged path reads the source **twice** — Phase 1 walks the
server to stage references, Phase 2 re-fetches every resource by ID. At scale
that doubles the dependency on the source system and its availability.

## Goal

A production ingest path that reads FHIR NDJSON directly from local disk or S3,
reusing the existing engine so NLP, gPAS, scoring, the PII output gate and the
score gate all still apply. Unblocks the 1.4M run and is the foundation for the
terabyte roadmap.

## Non-goals

- Replacing `pipeline/sources/` (the *format* seam). A file source yields FHIR
  resources, which `process_data_batch` consumes natively; a format adapter here
  would be a layer that does nothing.
- Writing results back to a FHIR server as part of this work. Output continues
  to flow through `publish_result` to S3.
- Compressed (`.gz`) NDJSON. Deferred until a dataset requires it.

## Approach

Phase 1 scans each file once and stages a **byte offset** per resource instead
of a resource ID. Phase 2 seeks directly to that offset.

Two alternatives were considered and rejected:

- **Encrypted body staging** (reuse `MEDANON_STAGE_BODIES=encrypted`): almost no
  new code, but writes encrypted PHI at rest and would add ~1.6 GB to app-db,
  contradicting the deliberate design that the staging table holds no patient
  data.
- **Stream path only, resume by line offset**: simplest, but no partitioning,
  so no `--scale worker=N` and no process executor — it forfeits the
  parallelism that makes 1.4M tractable.

Byte-offset staging is the only option that preserves the no-PHI-at-rest
property, **removes the double-read** rather than porting it, and keeps
partitioned parallel execution.

## Architecture

New code is *transport*, not format, so it sits beside `integrations/fhir/`
(itself `bulk.py` + `reader.py` for the HTTP source). This keeps the
import-linter layering intact: `pipeline/` may import `integrations/`, never the
reverse.

### New package: `integrations/filesource/`

| Module | Responsibility | Depends on |
|---|---|---|
| `backends.py` | `ObjectBackend` Protocol plus `LocalBackend` and `S3Backend`. Four operations: `list(prefix)`, `stat(uri)`, `open_stream(uri)`, `read_range(uri, offset, length)`. S3 uses HTTP `Range`; local uses `seek`. | minio client (existing dependency) |
| `guard.py` | Fail-closed validation of a `source_uri`: allowed scheme, allow-listed base prefix, no traversal, resolved-realpath containment. Mirrors `api/deps.py::_validate_server_url` and the SQL-source host allow-list. | — |
| `discovery.py` | `source_uri` → ordered `[(file_uri, resource_type)]`. Globs `*.ndjson`, infers type from the filename stem (`Observation.000.ndjson` → `Observation`), skips `log.ndjson`, applies `resource_type` / `type_filter` scoping. | `backends`, `guard` |
| `reader.py` | `iter_records(file_uri)` → `(offset, length, resource_dict)` for Phase 1; `read_record(file_uri, offset, length)` → `dict` for Phase 2. Owns NDJSON line framing and the fingerprint check. | `backends` |

### Changed, minimally

- **`integrations/staging/store.py`** — idempotent DDL adding `source_uri TEXT`,
  `byte_offset BIGINT`, `byte_length INT`, `source_fingerprint TEXT` to
  `staged_resources`, following the existing `_PARTITION_DDL` migration pattern.
- **`pipeline/jobs/staged_worker/_core.py::_fetch_staged_resources`** — a third
  branch. It already branches `resource_blob` (decrypt in-process) → else FHIR
  re-fetch; add `byte_offset is not None` → `reader.read_record(...)`.
- **`pipeline/jobs/executor_file.py`** — new executor. Phase 1 scans and stages
  offsets, then delegates to the **existing** `_run_staged_phase2*` functions
  unchanged.
- **`api/routers/jobs_submit.py`, `api/schemas/jobs.py`** —
  `POST /v1/jobs/file-export`.

### Input contract

Prefix/directory scan. The user supplies a `source_uri`; MedAnon discovers
`*.ndjson` beneath it and infers each file's resource type from its filename
stem, matching the FHIR Bulk Data output convention.

```
POST /v1/jobs/file-export
  { "source_uri": "file:///data/fhir-datasets/sample-bulk-fhir-datasets-1000-patients",
    "config_profile": "value-masking" }
  { "source_uri": "s3://bucket/exports/2026-07/" }
```

Optional `resource_type` / `type_filter` scope the scan. `destination_id`
behaves as it does for other export jobs.

## Data flow

### Phase 1 — scan and stage offsets (new)

```
source_uri ──guard──▶ discovery ──▶ [(file_uri, resourceType), ...]
                                          │
                    per file: backend.stat() → fingerprint (size + mtime | ETag)
                                          │
                    reader.iter_records(file) streams the file once, tracking
                    byte position, yielding (offset, length, resource_dict)
                                          │
                    batch INSERT into staged_resources every 1000 rows:
                      (job_id, resource_id, resource_type,
                       source_uri, byte_offset, byte_length, source_fingerprint)
                                          │
                    checkpoint {file_index, byte_offset, staged_count}
                                          ▼
                              plan_partitions()   ← existing, 50k buckets
```

One sequential pass over the dataset. No PHI is written — offsets and types
only.

### Phase 2 — existing code, one new branch

```
claim_next_partition()  ← unchanged, FOR UPDATE SKIP LOCKED
        │
iter_partition() → rows carrying (source_uri, byte_offset, byte_length, fingerprint)
        │
_fetch_staged_resources():
    row.resource_blob   → decrypt in-process       (existing)
    row.byte_offset     → reader.read_record(...)  (NEW: seek, no HTTP)
    else                → FHIR re-fetch by id      (existing)
        │
process_data_batch → shard file → merge → publish_result (score gate) → S3
```

Everything from `process_data_batch` onward is the current path unchanged,
including the F1 score-state handoff and the F2 atomic shard merge.

### Correctness properties

- **Fingerprint enforcement.** Offsets are valid only against the exact bytes
  Phase 1 scanned. The fingerprint is backend-specific and produced by
  `backend.stat()`: `LocalBackend` returns `"{size}:{mtime_ns}"`, `S3Backend`
  returns the object's `ETag`. Phase 2 stats each file **once per partition**
  (cached per process, not per record) and compares against the stored value. A
  mismatch raises and fails the partition. Fail-closed and non-negotiable: a
  file rotated between phases would otherwise emit records sliced at the wrong
  boundaries.
- **Crash resume.** Phase 1 checkpoints `(file_index, byte_offset)` and resumes
  by seeking, re-reading at most one partial batch. Phase 2 resume is unchanged —
  the partition ledger already handles it.
- **Determinism.** Files in discovery order, records in byte order within a
  file. The same input always yields the same shard boundaries and output
  ordering.
- **Cross-file references.** Unchanged. Resources reference each other across
  files exactly as they do across HAPI pages; `rewrite_references` and the gPAS
  cache resolve them identically.

## Failure handling

| Failure | Behaviour |
|---|---|
| Bad `source_uri` (scheme, traversal, outside allow-list) | Rejected at submit with 422, before a job exists |
| Fingerprint mismatch at Phase 2 | Partition fails → `release_partition` → retry → dead-letter |
| Malformed JSON line during scan | Staged as an error row and counted; job fails closed once errors exceed `MEDANON_FILESOURCE_MAX_BAD_LINES` |
| Truncated final line (no trailing newline) | Treated as corrupt, not as a record — the classic partial-write signature |
| File missing or size-changed at Phase 2 | Same as fingerprint mismatch |
| S3 transient error | Bounded retry with backoff in `backends.py`; exhausted → partition fails, ledger retries |
| Empty dataset | Job completes with 0 resources and no score — the existing legitimate "nothing to check" case |

## Testing

1. **Unit — `guard.py`**: traversal (`../`), symlink escape, disallowed scheme,
   non-allow-listed prefix. All must reject.
2. **Unit — `discovery.py`**: type inference, `log.ndjson` skipped,
   `resource_type` / `type_filter` scoping, deterministic ordering.
3. **Unit — `reader.py`**: offsets exact across UTF-8 multibyte, CRLF, blank
   lines and very long lines; `read_record(offset, length)` round-trips to the
   dict `iter_records` produced. This is the correctness core — wrong offsets
   make everything downstream silently wrong.
4. **Unit — fingerprint**: mutate the file between scan and read → must raise,
   not return data.
5. **Integration — crash resume**: interrupt Phase 1 mid-file, restart, assert
   no duplicate and no missing rows.
6. **Equivalence (acceptance gate)**: load a small NDJSON fixture into HAPI, run
   the same data through both the HAPI source and the file source with the same
   profile, assert the de-identified outputs are identical. This proves the new
   source is a true drop-in and that gates and scoring behave the same.
7. **Scale smoke**: the full 1,441,881-resource run, asserting the score gate
   computes over the whole set and the merged output line count matches.

## Sequencing

`LocalBackend` is implemented and tested first; `S3Backend` lands immediately
after behind the same `ObjectBackend` interface. The 1.4M dataset is local, so
shipping S3 before it can be tested against real data would be speculative.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MEDANON_FILESOURCE_ALLOWED_PREFIXES` | *(empty — deny all)* | Comma-separated allow-list of base URIs a `source_uri` must fall under. Empty denies every request. |
| `MEDANON_FILESOURCE_MAX_BAD_LINES` | `100` | Malformed lines tolerated before the job fails closed |

## Consequences

- The 1.4M dataset becomes processable against ~2 GB of disk instead of ~33 GB.
- The staged path's double-read is eliminated for file sources: Phase 2 becomes
  a local seek or a ranged GET instead of an HTTP re-fetch.
- HAPI is no longer required to be the source of truth for large runs.
- New surface area: a user-supplied path reaching the filesystem. The allow-list
  guard is the control, and it denies by default.
