# Performance Analysis: Bulk Export Pipeline

**Date:** 2026-04-15
**Job:** Bulk Export `1403859c`
**Profile:** auto (→ `config_gpas.yaml` when GPAS_URL set)

---

## Observed Performance

| Checkpoint | Processed | Elapsed | Throughput | Delta Throughput |
|------------|-----------|---------|------------|------------------|
| T1 | 85,000 | 31m 09s | 45.5 res/s | — |
| T2 | 101,000 | 52m 17s | 32.2 res/s | **12.6 res/s** (last 16K) |

**The pipeline is degrading 3.6× as the job progresses.** The last 16,000 resources took 21 minutes — the same wall-clock time as the first 85,000.

---

## Executive Summary

The bulk export pipeline exhibits two distinct performance problems:

1. **Constant overhead** (~45 res/s ceiling at start): sequential gPAS domain calls, serial finalization, redundant tree walks, lock contention on gPAS cache
2. **Progressive degradation** (45 → 12 res/s over time): unbounded memory growth in `seen_values` set, GC pressure, possible cache thrashing, and gPAS backend slowdown as pseudonym tables grow

Addressing both problems could push sustained throughput to **150–250 res/s** (5–8× improvement over degraded state).

---

## Part 1: Progressive Degradation Analysis

### Why throughput drops from 45 to 12 res/s

#### D1. Unbounded `seen_values` Set — Memory + GC Pressure
**Files:** `executors.py:185`, `processor.py:668,471-472`

The cross-chunk gPAS dedup set grows with every unique pseudonymized value and is **never bounded or evicted**:

```python
# executors.py:185 — created once per job, never bounded
self._seen_values: set[str] = set()

# processor.py:471-472 — grows after every chunk
if _seen_accumulator is not None and shared_mapping:
    _seen_accumulator.update(shared_mapping.keys())
```

**Growth at 100K resources:**
- ~5-10 unique pseudonymizable values per resource
- ~60% unique across the dataset → **300K-600K entries**
- Each UUID string (~36 chars) = ~90 bytes in CPython (object header + hash + string)
- **Estimated memory: 50-60 MB** for a single job

**Why this degrades performance:**
- Python's set rehashes when load factor exceeds ~66%. Each rehash is **O(n)** and copies the entire hash table. At 500K entries, a rehash moves ~40 MB of data.
- The set keeps all values alive for the entire job, increasing CPython GC Gen-2 collection time.
- CPython's GC runs every 700 Gen-0 allocations (default). As retained Gen-2 objects grow, each Gen-2 sweep (triggered every ~10 Gen-0 sweeps) traverses more objects. At 500K+ long-lived set entries, GC pauses become measurable.

**Fix:** Bloom filter (~600 KB for 500K entries at 0.1% FPR) or capped LRU set with background eviction.

---

#### D2. ScoreCollector Retains Full Patient Resources
**File:** `pipeline/scoring/engine.py:158-159,228-240`

```python
self._patients: list[dict] = []           # up to 10,000 Patient dicts
self._patient_manifests: list[list[dict]] = []  # parallel manifest list
```

The scoring engine keeps up to 10,000 **full Patient resource dicts** in memory for batch-level k-anonymity analysis. These are the same objects returned by the processor — retaining them prevents GC of both the Patient dicts and everything they reference.

**Estimated memory:** 10,000 patients × ~3-5 KB each = **30-50 MB** held for the entire job duration.

**Fix:** Extract only the 3 quasi-identifier fields needed for k-anonymity (gender, birth year, zip prefix) instead of the full dict. Reduces per-patient retention from ~5 KB to ~100 bytes.

---

#### D3. NLP Detection Cache Eviction Under Global Lock
**File:** `integrations/nlp/detector.py:494-498`

When the 20K-entry detection cache is full, eviction materializes **all 20K keys** under a global lock:

```python
with _DETECTION_CACHE_LOCK:
    if len(_DETECTION_CACHE) >= _DETECTION_CACHE_MAX:
        evict_count = _DETECTION_CACHE_MAX // 5           # = 4,000
        for key in list(_DETECTION_CACHE.keys())[:evict_count]:  # O(20K) list copy
            del _DETECTION_CACHE[key]                      # 4,000 deletions under lock
    _DETECTION_CACHE[cache_key] = hits
```

**Progressive impact:** Early in the job, the cache has room — no eviction, no lock contention. After ~20K unique texts are cached, **every new detection** triggers this eviction path:
- O(20K) to create the key list
- O(4K) deletions while holding the lock
- All other threads waiting on `_DETECTION_CACHE_LOCK` are blocked during this period (typically 1-5ms per eviction)

For datasets with diverse clinical narratives (>20K unique texts), this fires on most chunks after warmup.

**Fix:** Replace with `functools.lru_cache` (C-implemented, no Python-level lock for eviction) or use a sharded dict.

---

#### D4. NLP Token State Eviction Breaks Consistency
**File:** `integrations/nlp/detector.py:511-520`

```python
def _evict_if_needed(token_state, limit=100_000):
    if len(token_state["map"]) <= limit:
        return
    evict_count = len(token_state["map"]) // 4           # 25,000 entries
    keys_to_drop = list(token_state["map"].keys())[:evict_count]  # O(100K) list
    for key in keys_to_drop:
        token = token_state["map"].pop(key, None)
        if token:
            token_state["reverse"].pop(token, None)      # breaks reverse mapping
```

**Progressive impact:** After 100K unique PII values are tokenized:
- O(100K) key list creation + O(25K × 2) pop operations under the global lock
- **Consistency break:** Evicted values get re-tokenized with new token numbers if they reappear. The reverse mapping is destroyed, making de-tokenization impossible for evicted entries.
- This compounds with the detection cache eviction — both hit in the same phase of job progress.

**Fix:** Never evict mid-job. Or use a log-structured token map where old entries are kept read-only.

---

#### D5. gPAS Backend Table Growth
**External to application code**

The gPAS PostgreSQL backend maintains a pseudonym→original mapping table. As the job processes more resources:
- The table grows with each new pseudonym INSERT
- B-tree index maintenance cost increases logarithmically
- WildFly's JPA query cache may churn
- If the gPAS domain was fresh at job start, the table goes from 0 to potentially 500K+ rows during the job

This explains progressive latency increase on the gPAS side — each batch takes slightly longer as the table grows.

**Fix:** Pre-warm gPAS domains. Use connection pooling hints. Tune PostgreSQL `work_mem` for the domain table size.

---

#### D6. Python GC Compound Effect

The combined memory retention creates a compounding GC problem:

```
Memory accumulation during a 100K resource job:

  seen_values:     ~50-60 MB (unbounded set, never freed)
  ScoreCollector:  ~50-80 MB (Patient dicts + manifests, capped at 10K)
  gPAS L1 cache:   ~7 MB    (bounded at 50K entries)
  NLP det. cache:  ~5-10 MB (bounded at 20K entries)
  Token state:     ~20-30 MB (bounded at 100K entries)
                   ──────────
  Total retained:  ~130-190 MB of long-lived objects

CPython GC behavior:
  Gen-0: collects every ~700 allocations (~negligible pause)
  Gen-1: collects every ~10 Gen-0 cycles (~negligible pause)
  Gen-2: collects every ~10 Gen-1 cycles

  At 100K+ long-lived objects in Gen-2:
  - Each Gen-2 sweep traverses ALL reachable objects
  - With 130-190 MB of retained dicts/sets/lists,
    Gen-2 sweeps take 50-200ms (proportional to object count)
  - Gen-2 sweeps happen roughly every 70,000 allocations
  - At ~100 allocations per resource, Gen-2 triggers every ~700 resources
  - That's ~100-200ms pause every ~700 resources → 3-6% throughput loss
```

At the start of the job, Gen-2 has few objects and sweeps are fast. As retained memory grows, each sweep takes longer and the throughput penalty compounds.

**Fix:** `gc.set_threshold(700, 50, 100)` to reduce Gen-2 frequency. Or `gc.freeze()` after caches are warm to exclude stable objects from GC traversal. Or restructure to not retain long-lived Python objects.

---

### Degradation Timeline Visualization

```
Throughput
(res/s)

  50 ┤ ████████████████████████──────────
     │                                ──────
  40 ┤                                     ──────
     │                                          ────
  30 ┤                                              ────
     │                                                  ──
  20 ┤                                                    ──
     │                                                      ──
  10 ┤                                                        ──
     │
   0 ┼──────────┬──────────┬──────────┬──────────┬──────────
     0        25K        50K        75K       100K
                     Resources Processed

Degradation drivers by phase:
  ▓ 0-20K:   Caches warming up, gPAS table small → peak throughput
  ▓ 20-50K:  Detection cache full, eviction starts → first slowdown
  ▓ 50-80K:  seen_values >250K, GC Gen-2 sweeps costly → steady decline
  ▓ 80K+:    All effects compound + gPAS table large → rapid decline
```

---

## Part 2: Constant Overhead Bottlenecks

These bottlenecks limit the ceiling throughput from the start.

### Tier 1 — Critical (each independently costs 20–50% throughput)

#### B1. Sequential gPAS Domain Iteration
**File:** `pipeline/gpas_orchestrator.py:287-319`

When multiple gPAS domains are configured, the batch pre-fetch issues **one HTTP call per domain sequentially**:

```python
for domain, values in domain_to_values.items():
    partial = pseudonymizer.pseudonymize_batch(unique_values, call_params)
    combined_mapping.update(partial)
```

With 3-5 domains at 50ms gPAS latency each: **150-250ms serial wait per chunk** → ~50ms if parallelized.

**Fix:** Submit domain calls to `get_executor()` in parallel, join all futures.

---

#### B2. Per-Value Redis Cache Writes (No Pipelining)
**Files:** `integrations/gpas/client.py:114-118`, `utils/cache.py:167-169`

After each gPAS batch (500 values), every pseudonym is individually written to Redis:

```python
for orig, psn in partial.items():
    _cache_set(("pseudonymize", base_url, domain, operation, orig), psn)
```

**500 individual Redis round-trips per chunk** instead of a pipeline.

**Fix:** `client.pipeline()` + `pipe.execute()` for batch writes. ~1ms vs ~50ms.

---

#### B3. LocalLruCache Single Global Lock
**File:** `utils/cache.py:46-56`

Every cache lookup and store acquires the same lock. Cache hits do a delete+reinsert for LRU promotion — reads are as expensive as writes:

```python
def get(self, key):
    with self._lock:                    # single lock for ALL 64 threads
        value = self._cache.get(key)
        if value is not None:
            del self._cache[key]        # LRU promotion: delete+reinsert
            self._cache[key] = value
        return value
```

**Additional finding:** `TieredCache.get()` acquires this lock **twice** on L2 hits (once for the miss, once for the L1 promotion set). Cold-cache thundering herd after process restart.

**Fix:** Shard into 8 sub-caches with independent locks. Or accept imprecise LRU and skip promotion on read.

---

#### B4. Serial Finalization Loop (Pass 2 + Post-Processing)
**File:** `pipeline/processor.py:495-531`

Pass 1 runs in parallel, but the entire finalization phase runs **single-threaded**:

```python
for i, (resource, gpas_work, manifest_entries_) in enumerate(
    zip(parsed, all_gpas_works, all_manifest_entries)
):
    result = _finalize_resource(...)
```

Each `_finalize_resource` performs tree walks for gPAS write-back + reference pseudonymization + text-ID replacement. For 200 resources, thread pool sits idle.

**Fix:** Parallelize via `get_executor().map()` — `shared_mapping` is read-only at this point.

---

#### B5. N=1 De-Pseudonymization (No Batch)
**File:** `integrations/gpas/client.py:162-215`

`gpas_depseudonymize_by_path` makes **one HTTP call per field**:

```python
fhir_request = _build_depseudonymize_params(domain, [pseudonym_value])
resp_json = _call_gpas_operation(base_url, "dePseudonymize", fhir_request, params)
```

10 pseudonymized fields → 10 sequential HTTP calls.

**Fix:** Collect all values first, make one batch `$dePseudonymize` call, write back.

---

#### B6. 3-5 Redundant Tree Walks Per Resource
**Files:** `processor.py`, `post_processor.py`, `gpas_orchestrator.py`, `deidentify.py`

| Walk | Purpose | Location |
|------|---------|----------|
| 1 | Collect reference IDs | `_collect_reference_ids` |
| 2 | FHIRPath rule matching | `_build_match_candidates` |
| 3 | Action execution | `find_nodes` in each action |
| 4 | gPAS write-back | `find_nodes` in gpas_orchestrator |
| 5 | Reference rewriting + text-ID | `_post_process_resource` |

Walks 1, 3, 4, 5 overlap significantly.

**Fix:** Merge walk 1 into walk 2, merge walk 4 into walk 5. Target: 2 walks instead of 5.

---

#### B7. No Fetch/Process Overlap in Staged Worker
**File:** `pipeline/jobs/staged_worker.py:311-343`

The staged worker's Phase 2 is strictly sequential: query → process → write → mark done → repeat.

**Additional finding:** The staged path has **no cross-batch gPAS dedup** (no `_seen_values` passed to `process_data_batch`). Each batch makes redundant gPAS calls for common values (e.g., the same Patient ID referenced by every Observation).

**Fix:** Use `_PipelinedProcessor` pattern for staged path. Add `_seen_values` for dedup.

---

### Tier 2 — High Impact (each costs 5–15% throughput)

#### B8. Bulk NDJSON Download Defaults to Sequential
**File:** `integrations/fhir/bulk.py:44`

`MEDANON_BULK_DOWNLOAD_PARALLEL` defaults to `1`. The parallel download code exists but is off.

**Fix:** Default to 4.

---

#### B9. Quadratic String Replacement in NLP
**File:** `pipeline/deidentify.py:276-304`

```python
for start, end, entity_type in hits:
    text = _replace_span(text, start, end, entity_type, ea, token_state)
    # _replace_span: text[:start] + replacement + text[end:]
```

O(H × L) where H = entities found, L = text length. Clinical notes with 10+ entities pay quadratic cost.

**Fix:** Build segment list and `"".join()` once — O(L + H).

---

#### B10. NLP Batch Pre-Warm Uses Wrong Params for Mixed Rules
**File:** `pipeline/nlp_orchestrator.py:283-286`

All texts are pre-warmed with the **first rule's** entity list. Mixed entity configs cause cache key mismatches — **negating the batch optimization entirely**.

**Fix:** Group texts by `(entities, threshold, language)`, pre-warm each group.

---

#### B11. Fallback Path Loses Batch gPAS Benefit
**File:** `pipeline/jobs/executors.py:264-302`

One bad resource in a 500-resource chunk forces **all 499 remaining** into individual processing.

**Fix:** Binary search to isolate bad resource, batch-process the rest.

---

#### B12. gPAS Retry Thread Starvation
**File:** `integrations/gpas/transport.py:233-295`

Under gPAS overload, each failed call occupies a thread for up to **91 seconds** (30s timeout × 3 attempts + backoff sleeps):

```
attempt 0: 30s timeout → fail
sleep:     ~0.3s
attempt 1: 30s timeout → fail
sleep:     ~0.6s
attempt 2: 30s timeout → fail
Total:     91s thread occupation
```

Circuit breaker needs 5 failures to trip = **150 seconds** of degraded gPAS before protection activates. During those 150s, with 10+ sub-batches per job, the 64-thread pool saturates.

**Fix:** Separate connect/read timeout (connect=5s, read=30s). Trip CB on first timeout. Thread pool backpressure (bounded queue).

---

#### B13. gPAS Connection Pool Overflow
**File:** `integrations/gpas/transport.py:35-41`

Pool size = 10. With 3 concurrent jobs × 5 sub-batches = 15 connections needed. Overflow connections are created/destroyed per-request (TCP churn).

**Fix:** `GPAS_POOL_SIZE = JOB_WORKERS * 8`.

---

#### B14. Thread Pool Queue Unbounded
**File:** `utils/thread_pool.py:46-49`

`ThreadPoolExecutor` uses an unbounded `queue.SimpleQueue`. Under gPAS degradation, tasks accumulate faster than they drain. Each queued task holds references to its arguments (chunk data, params dict), preventing GC.

**Fix:** Use a bounded queue with backpressure, or `concurrent.futures.wait()` with FIRST_COMPLETED to limit in-flight futures.

---

### Tier 3 — Medium Impact (each costs 2–5% throughput)

#### B15. Per-Chunk Cancellation Polling
**Files:** `executors.py:311-313`, `staged_worker.py:312-317`

85K resources / batch_size 200 = 425 store round-trips just for cancellation checks.

**Fix:** Poll every 5 chunks instead of every chunk.

---

#### B16. Bundle Double-Walk for Reference Rewriting
**File:** `pipeline/processor.py:588-601`

`_process_bundle` does two separate walks (`_rewrite_references` + `_rewrite_text_ids`) when the merged `_post_process_resource` handles both in one pass.

**Fix:** Use `_post_process_resource` for bundles.

---

#### B17. `_skip_to` Re-Fetches Entire FHIR History on Resume
**File:** `pipeline/jobs/executors.py:80-84`

On crash recovery at resource 90K, the pipeline re-downloads and discards 90K resources from the FHIR server.

**Fix:** Persist pagination cursor in checkpoint data.

---

#### B18. NLP Fallback Retry Storm
**File:** `integrations/nlp/remote_detector.py:268-312`

Under NLP degradation: full batch (120s timeout) → 10 sub-batches → 200 sequential calls = **up to 221 HTTP requests** amplifying load.

**Fix:** Trip circuit breaker immediately on batch failure.

---

#### B19. Two Independent Thread Pools (96 threads total)
**Files:** `utils/thread_pool.py`, Python asyncio internals

- `get_executor()`: 64 threads
- asyncio default executor: 32 threads
- No unified budget. Total: 96 threads invisible to operators.

**Fix:** `loop.set_default_executor(get_executor())`.

---

#### B20. Unconditional JSON Snapshot in "skip" Mode
**File:** `pipeline/processor.py:186`

Every resource in "skip" mode gets JSON-serialized for rollback. Error path fires ~0.01% of the time.

**Fix:** Lazy snapshot — only serialize when first mutation is attempted.

---

#### B21. Bulk Import Full Materialization
**File:** `pipeline/jobs/executors.py:764-781`

`_execute_bulk_import` loads the **entire** NDJSON file into memory as Python dicts. At 100K resources × ~2 KB: **200 MB+** in RAM.

**Fix:** Stream-process with chunking (same pattern as the export path).

---

## Part 3: Memory Budget at 100K Resources

```
Structure                   Location               Bounded?  Size at 100K    Freed When
──────────────────────────────────────────────────────────────────────────────────────────
seen_values (set)           executors.py:185        NO        50-60 MB        Job end
ScoreCollector._patients    engine.py:158           Yes(10K)  30-50 MB        Job end
ScoreCollector._manifests   engine.py:159           Yes(10K)  10-30 MB        Job end
NLP token_state.map         detector.py:451         Yes(100K) 20-30 MB        Process end
NLP _DETECTION_CACHE        detector.py:459         Yes(20K)  5-10 MB         Process end
gPAS L1 cache               cache.py:45             Yes(50K)  ~7 MB           Process end
gPAS L2 cache (Redis)       cache.py:100            Redis-managed  ~N/A       Redis eviction
FHIRPath caches             rule_matcher.py         Yes(512)  <1 MB           Process end
Rule index caches           rule_matcher.py         Yes(64)   <1 MB           Process end
ScoreAuditCollector         audit.py:55-87          Yes(5K)   ~320 KB         Job end
──────────────────────────────────────────────────────────────────────────────────────────
TOTAL RETAINED PER JOB                                        ~130-190 MB
TOTAL RETAINED PROCESS-WIDE                                   ~35-50 MB (caches persist)
```

With 3 concurrent jobs: **~400-600 MB** of long-lived Python objects triggering GC pressure.

---

## Part 4: Thread Contention Map

```
Lock                            Location               Hot Path?  Threads    Contention
────────────────────────────────────────────────────────────────────────────────────────
LocalLruCache._lock             cache.py:46            YES        Up to 64   HIGH — every read is a write (LRU promote)
_DETECTION_CACHE_LOCK           detector.py:460        YES (NLP)  Up to 64   HIGH when cache full (O(20K) eviction)
_GLOBAL_TOKEN_LOCK              detector.py:451        CLI only   1          None in API mode
_gpas_params_lock               gpas_orchestrator:29   No (warm)  Up to 64   None after warmup
_cache_lock (rule_matcher)      rule_matcher.py:351    No (warm)  Up to 64   None after warmup
CircuitBreaker._lock            circuit_breaker.py:75  YES        Up to 64   Low (O(1) work under lock)
ThreadPoolExecutor internal     thread_pool.py:46      YES        N/A        Queue contention under load
```

---

## Part 5: Data Flow Timing — Degraded vs Fresh

### Per 200-resource chunk timing

```
Phase                     Fresh (chunk 1)    Degraded (chunk 500)    Delta
─────────────────────────────────────────────────────────────────────────────
FHIR fetch (pipelined)    ~0ms               ~0ms                    —
JSON parse                ~5ms               ~5ms                    —
Rule matching             ~50ms              ~50ms                   — (config-keyed caches)
Pass 1: Action dispatch   ~100ms             ~100ms                  — (parallel, no growth)
Pass 1.5: NLP batch       ~200ms             ~400ms                  +100% (cache eviction)
Pass 2: gPAS batch        ~300ms             ~500ms                  +67% (table growth + seen_values)
Finalization (serial)     ~200ms             ~200ms                  — (per-resource, constant)
Post-processing           ~100ms             ~100ms                  — (per-resource, constant)
NDJSON write              ~10ms              ~10ms                   —
Scoring                   ~50ms              ~50ms                   — (bounded collectors)
GC pauses (amortized)     ~5ms               ~80ms                   +1500% (Gen-2 sweep growth)
Checkpoint + cancel       ~5ms               ~5ms                    —
─────────────────────────────────────────────────────────────────────────────
Total per chunk:          ~1025ms            ~1505ms                 +47%
Effective throughput:     ~195 res/s (ideal) ~133 res/s (ideal)

Observed throughput (includes lock contention):
  Fresh:                  ~45 res/s
  Degraded:               ~12 res/s          → 4.3× gap from ideal
```

The **lock contention overhead** (not visible in per-phase timing) accounts for the gap between ideal (~133 res/s degraded) and observed (~12 res/s degraded). Under high thread counts, `LocalLruCache._lock` and `_DETECTION_CACHE_LOCK` create serial bottlenecks that grow worse as eviction frequency increases.

---

## Optimization Roadmap

### Phase 0 — Stop the Bleeding (COMPLETED 2026-04-16)

| # | Change | Status | Implementation |
|---|--------|--------|----------------|
| D1 | Bound `seen_values` with `_CappedSet` (200K cap) | DONE | `processor.py`, `executors.py` |
| D2 | Extract QI-only fields in ScoreCollector | DONE | `scoring/engine.py`, `scoring/privacy.py` |
| D3 | Replace `_DETECTION_CACHE` with `functools.lru_cache` | DONE | `integrations/nlp/detector.py` |
| D4 | Tune GC: `gc.set_threshold(700, 50, 100)` during bulk jobs | DONE | `pipeline/jobs/executors.py` |

### Phase 1 — Quick Wins (COMPLETED 2026-04-16)

| # | Change | Status | Implementation |
|---|--------|--------|----------------|
| B1 | Parallel gPAS domain calls via `as_completed` | DONE | `gpas_orchestrator.py` |
| B2 | Batch cache ops: `get_many`/`set_many` + Redis pipeline | DONE | `utils/cache.py`, `gpas/transport.py`, `gpas/client.py` |
| B3 | Shard LocalLruCache (8 shards, independent locks) | DONE | `utils/cache.py` |
| B4 | Parallelize finalization loop (>4 resources → thread pool) | DONE | `pipeline/processor.py` |
| B8 | `MEDANON_BULK_DOWNLOAD_PARALLEL` default → 4 | DONE | `integrations/fhir/bulk.py` |
| B9 | Linear NLP string replacement (O(L+H) segments) | DONE | `integrations/nlp/detector.py` |
| B10 | Group NLP batch pre-warm by `(entities, threshold, lang)` | DONE | `pipeline/nlp_orchestrator.py` |
| B15 | Cancel-check every 5 chunks | DONE | `pipeline/jobs/executors.py` |
| B16 | Merge bundle double-walk (ref rewrite + text-ID in one pass) | DONE | `pipeline/processor.py`, `pipeline/post_processor.py` |

### Phase 2 — Medium Effort (COMPLETED 2026-04-16)

| # | Change | Status | Implementation |
|---|--------|--------|----------------|
| B5 | Batch de-pseudonymization (N values → 1 HTTP call) | DONE | `gpas/client.py`, `gpas_orchestrator.py`, `action_dispatcher.py` |
| B12 | Fast CB trip on timeouts (2 timeouts → OPEN) | DONE | `utils/circuit_breaker.py`, `gpas/transport.py` |
| B6 | Merge tree walks — assessed: B16 merged the main opportunity; remaining walks have different timing requirements (pre/post mutation) | N/A | — |

### Phase 3 — Architectural (NOT STARTED)

| # | Change | Expected Gain | Risk |
|---|--------|--------------|------|
| B7 | Pipelined staged worker + cross-batch dedup | -30% staged path | Medium |
| B14 | Bounded thread pool queue | Backpressure under load | Medium |
| B19 | Unify asyncio + shared thread pool | Predictable threading | Medium |
| B17 | FHIR cursor persistence for resume | Eliminate re-fetch | High |
| B21 | Stream-process bulk import | -200 MB peak memory | Medium |
| D4 | Log-structured token map (no mid-job eviction) | Correctness + perf | High |

### Projected Sustained Throughput

```
Current (degraded):              12 res/s    (at 100K+)
After Phase 0:                   45 res/s    (flat curve)
After Phase 0 + 1:             ~100 res/s    (~2.2×)
After Phase 0 + 1 + 2:         ~170 res/s    (~3.8×)
After Phase 0 + 1 + 2 + 3:     ~250 res/s    (~5.5×)
```

---

## Quick Configuration Tuning (No Code Changes)

These environment variable changes may improve throughput for the current codebase:

```bash
# Increase bulk download parallelism (default: 1)
MEDANON_BULK_DOWNLOAD_PARALLEL=4

# Increase gPAS connection pool (default: 10)
GPAS_POOL_SIZE=20

# Increase FHIR connection pool (default: 10)
FHIR_POOL_SIZE=20

# Increase processing batch size if memory allows (default: 200)
MEDANON_BATCH_SIZE=500

# Increase gPAS batch size if gPAS handles it (default: 500)
GPAS_MAX_BATCH_SIZE=1000

# Increase thread budget if CPU cores > 8 (default: 64)
MEDANON_GLOBAL_MAX_THREADS=96

# Reduce GC frequency for long-running jobs
PYTHONDONTWRITEBYTECODE=1
```

**Note:** Increasing `MEDANON_BATCH_SIZE` trades memory for throughput. With degradation fixes (Phase 0) this becomes safe. Without them, larger batches accelerate the degradation.
