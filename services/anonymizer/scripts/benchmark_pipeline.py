#!/usr/bin/env python3
"""Pipeline performance benchmark.

Run from services/anonymizer/:
    python3 scripts/benchmark_pipeline.py

Exercises the hot paths and prints wall-clock timings + throughput so you
can compare before/after optimisation impact.

No Docker or external services required — uses mocked gPAS and synthetic FHIR.
"""

from __future__ import annotations

import copy
import json
import os
import re
import resource as _resource
import sys
import time

# Make src/ importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Python 3.13 removed typing.io; fhirpathpy (via antlr4) still imports it.
# Shim it the same way conftest.py does for the test suite.
try:
    import typing.io  # noqa: F401
except (ImportError, ModuleNotFoundError):
    import types as _types
    import typing as _typing
    _io_shim = _types.ModuleType("typing.io")
    _io_shim.TextIO = _typing.TextIO  # type: ignore[attr-defined]
    _io_shim.BinaryIO = _typing.BinaryIO  # type: ignore[attr-defined]
    sys.modules["typing.io"] = _io_shim

# Suppress audit logging noise
import logging
logging.disable(logging.CRITICAL)

# Must set before importing pipeline modules
os.environ.setdefault("MEDANON_HASH_ALLOW_PLAIN", "true")
os.environ.setdefault("GPAS_URL", "")


# ---------------------------------------------------------------------------
# Synthetic FHIR data generators
# ---------------------------------------------------------------------------

def _make_patient(i: int) -> dict:
    return {
        "resourceType": "Patient",
        "id": f"pat-{i:06d}",
        "meta": {"lastUpdated": "2024-01-15T10:30:00Z", "versionId": "1"},
        "text": {"status": "generated", "div": f"<div>Patient pat-{i:06d} record</div>"},
        "name": [
            {"use": "official", "family": f"Family{i}", "given": [f"Given{i}", "M"]},
            {"use": "nickname", "text": f"Nick{i}"},
        ],
        "identifier": [
            {"system": "urn:oid:1.2.3", "value": f"MRN-{i:06d}"},
            {"system": "http://hospital.org/id", "value": f"H-{i:06d}"},
        ],
        "birthDate": f"19{60 + i % 40}-{1 + i % 12:02d}-{1 + i % 28:02d}",
        "gender": "male" if i % 2 == 0 else "female",
        "address": [{"line": [f"{i} Main St"], "city": "Springfield", "state": "IL", "postalCode": "62701"}],
        "telecom": [
            {"system": "phone", "value": f"+1-555-{i:04d}"},
            {"system": "email", "value": f"patient{i}@example.com"},
        ],
        "generalPractitioner": [{"reference": f"Practitioner/prac-{i % 50:04d}", "display": f"Dr. {i}"}],
        "managingOrganization": {"reference": "Organization/org-001"},
        "extension": [
            {"url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
             "extension": [
                 {"url": "ombCategory", "valueCoding": {"code": "2106-3", "display": "White"}},
                 {"url": "text", "valueString": "White"},
             ]},
            {"url": "http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName",
             "valueString": f"Maiden{i}"},
            {"url": f"http://synthetichealth.github.io/synthea/disability-adjusted-life-years",
             "valueDecimal": 65.2},
        ],
    }


def _make_observation(i: int) -> dict:
    return {
        "resourceType": "Observation",
        "id": f"obs-{i:06d}",
        "meta": {"lastUpdated": "2024-02-01T08:00:00Z"},
        "text": {"status": "generated", "div": f"<div>Observation for pat-{i % 1000:06d}</div>"},
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9", "display": "Blood pressure"}]},
        "subject": {"reference": f"Patient/pat-{i % 1000:06d}", "display": f"Patient {i}"},
        "performer": [{"reference": f"Practitioner/prac-{i % 50:04d}"}],
        "valueQuantity": {"value": 120 + i % 40, "unit": "mmHg"},
        "effectiveDateTime": f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}T09:00:00Z",
    }


def _make_encounter(i: int) -> dict:
    return {
        "resourceType": "Encounter",
        "id": f"enc-{i:06d}",
        "meta": {"lastUpdated": "2024-03-01T12:00:00Z"},
        "text": {"status": "generated", "div": f"<div>Encounter enc-{i:06d} for pat-{i % 1000:06d}</div>"},
        "status": "finished",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": f"Patient/pat-{i % 1000:06d}", "display": f"Patient {i}"},
        "participant": [
            {"individual": {"reference": f"Practitioner/prac-{i % 50:04d}", "display": f"Dr. {i}"}},
        ],
        "period": {"start": f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}T08:00:00Z"},
    }


def _make_mixed_batch(n: int) -> list[dict]:
    """Generate a mixed batch of Patient, Observation, and Encounter resources."""
    resources = []
    for i in range(n):
        if i % 3 == 0:
            resources.append(_make_patient(i))
        elif i % 3 == 1:
            resources.append(_make_observation(i))
        else:
            resources.append(_make_encounter(i))
    return resources


# ---------------------------------------------------------------------------
# Mock settings (no disk I/O)
# ---------------------------------------------------------------------------

class BenchSettings:
    filename = "benchmark"
    processing_errors = "raise"
    rewrite_references = True
    rewrite_text_ids = True
    dynamic_rule_settings = {}
    rules = [
        {"name": "redact_name",       "match": "Patient.name",       "action": "redact", "params": {}},
        {"name": "redact_address",    "match": "Patient.address",    "action": "redact", "params": {}},
        {"name": "redact_telecom",    "match": "Patient.telecom",    "action": "redact", "params": {}},
        {"name": "redact_birthdate",  "match": "Patient.birthDate",  "action": "redact", "params": {}},
        {"name": "hash_id",           "match": "*.identifier",       "action": "redact", "params": {}},
        {"name": "scrub_text",        "match": "*.text.div",         "action": "redact", "params": {}},
        {"name": "redact_meta",       "match": "*.meta.lastUpdated", "action": "redact", "params": {}},
        {"name": "redact_display",    "match": "*.subject.display",  "action": "redact", "params": {}},
        {"name": "redact_performer",  "match": "*.performer.display","action": "redact", "params": {}},
        {"name": "redact_race_ext",  "match": "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race')", "action": "redact", "params": {}},
        {"name": "redact_synthea",   "match": "Patient.extension.where(url.startsWith('http://synthetichealth.github.io/synthea/'))", "action": "redact", "params": {}},
    ]


# ---------------------------------------------------------------------------
# Mock pseudonymizer (instant, no HTTP)
# ---------------------------------------------------------------------------

class MockPseudonymizer:
    def pseudonymize_batch(self, values, params):
        return {v: f"pseudo-{hash(v) % 999999:06d}" for v in values}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_rate(count, elapsed):
    rate = count / elapsed if elapsed > 0 else float("inf")
    return f"{rate:,.0f} resources/sec"


def _get_rss_mb() -> float:
    """Current RSS in MB (Linux/macOS)."""
    ru = _resource.getrusage(_resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return ru.ru_maxrss / 1_048_576  # bytes on macOS
    return ru.ru_maxrss / 1024  # KB on Linux


def _print_bar(label, value, max_val, width=40):
    """Print a simple text bar chart line."""
    bar_len = int((value / max_val) * width) if max_val > 0 else 0
    bar = "=" * bar_len
    print(f"  {label:>20}: {bar:<{width}} {value:,.0f}")


# ---------------------------------------------------------------------------
# Benchmark 1: FHIRPath — fast-path vs fhirpathpy
# ---------------------------------------------------------------------------

def bench_fhirpath():
    from pipeline.rule_matcher import _classify_match, _evaluate_simple_path, _evaluate_where_path

    expressions = [
        # Simple (typed dot-paths) — fast-path eligible
        "Patient.name", "Patient.birthDate", "Patient.address", "Patient.telecom",
        "Patient.identifier", "Observation.subject", "Observation.code",
        "Encounter.subject", "Encounter.participant",
        # Wildcard — fast-path eligible
        "*.id", "*.meta.lastUpdated", "*.text.div",
        # Complex FHIRPath — NOT fast-path eligible
        "Patient.name.where(use='official')",
        "Observation.value.ofType(Quantity)",
    ]

    # .where(url=...) expressions — native evaluator
    where_expressions = [
        "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race')",
        "Patient.extension.where(url='http://hl7.org/fhir/StructureDefinition/patient-mothersMaidenName').valueString",
        "Patient.extension.where(url='http://hl7.org/fhir/us/core/StructureDefinition/us-core-race').extension.where(url='text').valueString",
        "Patient.extension.where(url.startsWith('http://synthetichealth.github.io/synthea/'))",
    ]

    # Classify all expressions
    simple = [e for e in expressions if _classify_match(e) in ("simple", "wildcard")]
    complex_ = [e for e in expressions if _classify_match(e) == "fhirpath"]
    print(f"  Expressions: {len(simple)} simple/wildcard, {len(complex_)} complex fhirpath")

    resource = _make_patient(1)

    # --- Fast-path: _evaluate_simple_path ---
    N_FAST = 50_000
    start = time.perf_counter()
    for _ in range(N_FAST):
        for expr in simple:
            _evaluate_simple_path(resource, expr)
    fast_elapsed = time.perf_counter() - start
    fast_total = N_FAST * len(simple)
    fast_rate = fast_total / fast_elapsed

    # --- fhirpathpy on the SAME expressions (without .log() suffix) ---
    # Only include expressions fhirpathpy can parse (typed paths, not wildcards)
    import fhirpathpy
    fhirpath_parseable = [e for e in simple if not e.startswith("*")]
    compiled = {}
    for e in fhirpath_parseable:
        try:
            compiled[e] = fhirpathpy.compile(e)
        except Exception:
            pass  # skip expressions fhirpathpy can't handle
    if not compiled:
        print("  fhirpathpy: no expressions parseable, skipping comparison")
        return fast_rate, 1  # avoid div-by-zero

    N_SLOW = 2_000
    start = time.perf_counter()
    for _ in range(N_SLOW):
        for expr in compiled:
            compiled[expr](resource)
    slow_elapsed = time.perf_counter() - start
    slow_total = N_SLOW * len(compiled)
    slow_rate = slow_total / slow_elapsed

    # For fair comparison, also run fast-path on just the fhirpath-parseable subset
    N_FAST_FAIR = N_FAST
    start = time.perf_counter()
    for _ in range(N_FAST_FAIR):
        for expr in compiled:
            _evaluate_simple_path(resource, expr)
    fast_fair_elapsed = time.perf_counter() - start
    fast_fair_total = N_FAST_FAIR * len(compiled)
    fast_fair_rate = fast_fair_total / fast_fair_elapsed

    speedup = fast_fair_rate / slow_rate if slow_rate > 0 else float("inf")
    print(f"  fast-path (dict traversal): {fast_total:>10,} evals in {fast_elapsed:.3f}s = {fast_rate:>12,.0f} evals/sec")
    print(f"  fhirpathpy (interpreter)  : {slow_total:>10,} evals in {slow_elapsed:.3f}s = {slow_rate:>12,.0f} evals/sec")
    print(f"  >>> Fast-path is {speedup:.0f}x faster (on {len(compiled)} shared expressions)")

    # --- Native .where(url=...) evaluator ---
    where_native = [e for e in where_expressions if _classify_match(e) == "where"]
    if where_native:
        N_WHERE = 20_000
        start = time.perf_counter()
        for _ in range(N_WHERE):
            for expr in where_native:
                _evaluate_where_path(resource, expr)
        where_elapsed = time.perf_counter() - start
        where_total = N_WHERE * len(where_native)
        where_rate = where_total / where_elapsed
        print(f"  native .where() evaluator : {where_total:>10,} evals in {where_elapsed:.3f}s = {where_rate:>12,.0f} evals/sec")
    else:
        where_rate = 0
        print("  native .where() evaluator : no expressions classified as 'where'")

    return fast_fair_rate, slow_rate


# ---------------------------------------------------------------------------
# Benchmark 2: Text-ID replacement — Aho-Corasick vs regex
# ---------------------------------------------------------------------------

def bench_text_id():
    from pipeline.post_processor import _build_text_id_automaton, _aho_replace, _HAS_AHO

    results = []
    for map_size in [10, 100, 500, 1000, 5000]:
        id_map = {f"id-{i:06d}": f"pseudo-{i:06d}" for i in range(map_size)}

        automaton = _build_text_id_automaton(id_map)
        parts = [re.escape(k) for k in id_map]
        regex = re.compile(r"\b(" + "|".join(parts) + r")\b")

        # Text with ~10 embedded IDs spread across it
        text_ids = [f"id-{i:06d}" for i in range(0, map_size, max(1, map_size // 10))]
        text = "Patient " + " was referred to ".join(text_ids) + " at the clinic on 2024-01-15."

        N = max(500, 5000 // max(1, map_size // 100))

        # Regex
        start = time.perf_counter()
        for _ in range(N):
            regex.sub(lambda m: id_map[m.group(1)], text)
        regex_elapsed = time.perf_counter() - start
        regex_rate = N / regex_elapsed

        # Aho-Corasick
        if automaton and _HAS_AHO:
            start = time.perf_counter()
            for _ in range(N):
                _aho_replace(text, automaton, id_map)
            aho_elapsed = time.perf_counter() - start
            aho_rate = N / aho_elapsed
            speedup = aho_rate / regex_rate if regex_rate > 0 else float("inf")
            results.append((map_size, regex_rate, aho_rate, speedup))
            print(f"  IDs={map_size:>5}: regex={regex_elapsed:.3f}s  aho={aho_elapsed:.3f}s  ({speedup:.1f}x)")
        else:
            results.append((map_size, regex_rate, 0, 0))
            print(f"  IDs={map_size:>5}: regex={regex_elapsed:.3f}s  aho=N/A (pyahocorasick not installed)")

    return results


# ---------------------------------------------------------------------------
# Benchmark 3: Merged vs separate post-processing walks
# ---------------------------------------------------------------------------

def bench_post_processing():
    from pipeline.post_processor import (
        _apply_reference_pseudonyms, _rewrite_text_ids,
        _post_process_resource, _build_text_id_automaton,
    )

    N_RESOURCES = 2000
    ref_mapping = {f"pat-{i:06d}": f"pseudo-{i:06d}" for i in range(N_RESOURCES)}
    id_map = {f"pat-{i:06d}": f"pseudo-{i:06d}" for i in range(N_RESOURCES)}
    automaton = _build_text_id_automaton(id_map)

    # Separate walks (old approach)
    resources = [_make_observation(i) for i in range(N_RESOURCES)]
    start = time.perf_counter()
    for r in resources:
        _apply_reference_pseudonyms(r, ref_mapping)
        _rewrite_text_ids(r, id_map, automaton=automaton)
    separate_elapsed = time.perf_counter() - start

    # Merged walk (new approach)
    resources = [_make_observation(i) for i in range(N_RESOURCES)]
    start = time.perf_counter()
    for r in resources:
        _post_process_resource(r, ref_mapping, id_map, automaton=automaton)
    merged_elapsed = time.perf_counter() - start

    speedup = separate_elapsed / merged_elapsed if merged_elapsed > 0 else float("inf")
    sep_rate = N_RESOURCES / separate_elapsed
    merge_rate = N_RESOURCES / merged_elapsed
    print(f"  {N_RESOURCES:,} resources:")
    print(f"    separate walks: {separate_elapsed:.3f}s ({sep_rate:,.0f} resources/sec)")
    print(f"    merged walk:    {merged_elapsed:.3f}s ({merge_rate:,.0f} resources/sec)")
    print(f"    >>> Merged is {speedup:.2f}x faster")

    return sep_rate, merge_rate


# ---------------------------------------------------------------------------
# Benchmark 4: Full pipeline — process_data_batch at various batch sizes
# ---------------------------------------------------------------------------

def bench_full_pipeline():
    from pipeline.processor import process_data_batch
    from pipeline.rule_matcher import clear_rule_caches

    pseudonymizer = MockPseudonymizer()
    settings = BenchSettings()

    results = []
    for batch_size in [1, 10, 50, 100, 200, 500]:
        resources = _make_mixed_batch(batch_size)

        # Warm up caches
        clear_rule_caches()
        warmup = [copy.deepcopy(r) for r in resources[:3]]
        process_data_batch(warmup, settings, pseudonymizer)

        # Timed run
        rounds = max(1, 3000 // batch_size)
        start = time.perf_counter()
        for _ in range(rounds):
            batch = [copy.deepcopy(r) for r in resources]
            process_data_batch(batch, settings, pseudonymizer)
        elapsed = time.perf_counter() - start

        total = rounds * batch_size
        rate = total / elapsed if elapsed > 0 else 0
        results.append((batch_size, rate))
        print(f"  batch={batch_size:>4}: {total:>6,} resources in {elapsed:.3f}s = {rate:>8,.0f} resources/sec")

    return results


# ---------------------------------------------------------------------------
# Benchmark 5: Streaming pipeline
# ---------------------------------------------------------------------------

def bench_stream():
    from pipeline.processor import process_data_stream
    from pipeline.rule_matcher import clear_rule_caches

    pseudonymizer = MockPseudonymizer()
    settings = BenchSettings()
    clear_rule_caches()

    N = 3000

    def resource_gen():
        for i in range(N):
            if i % 3 == 0:
                yield _make_patient(i)
            elif i % 3 == 1:
                yield _make_observation(i)
            else:
                yield _make_encounter(i)

    start = time.perf_counter()
    count = sum(1 for _ in process_data_stream(resource_gen(), settings, pseudonymizer))
    elapsed = time.perf_counter() - start
    rate = count / elapsed if elapsed > 0 else 0

    print(f"  {count:,} resources in {elapsed:.3f}s = {rate:,.0f} resources/sec")
    return rate


# ---------------------------------------------------------------------------
# Benchmark 6: Memory — peak RSS during a large batch
# ---------------------------------------------------------------------------

def bench_memory():
    from pipeline.processor import process_data_batch
    from pipeline.rule_matcher import clear_rule_caches
    import gc

    pseudonymizer = MockPseudonymizer()
    settings = BenchSettings()
    clear_rule_caches()

    gc.collect()
    rss_before = _get_rss_mb()

    batch_size = 2000
    resources = _make_mixed_batch(batch_size)
    rss_after_gen = _get_rss_mb()

    results = process_data_batch([copy.deepcopy(r) for r in resources], settings, pseudonymizer)
    rss_after_proc = _get_rss_mb()

    del results
    del resources
    gc.collect()
    rss_after_gc = _get_rss_mb()

    print(f"  Baseline:         {rss_before:.1f} MB")
    print(f"  After generating: {rss_after_gen:.1f} MB (+{rss_after_gen - rss_before:.1f} MB for {batch_size} resources)")
    print(f"  After processing: {rss_after_proc:.1f} MB (+{rss_after_proc - rss_after_gen:.1f} MB pipeline overhead)")
    print(f"  After GC:         {rss_after_gc:.1f} MB")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    width = 70
    print("=" * width)
    print("  Pipeline Performance Benchmark".center(width))
    print(f"  Python {sys.version.split()[0]}  |  PID {os.getpid()}".center(width))
    print("=" * width)

    print("\n[1/6] FHIRPath evaluation: fast-path vs fhirpathpy")
    print("-" * width)
    fast_rate, slow_rate = bench_fhirpath()

    print(f"\n[2/6] Text-ID replacement: Aho-Corasick vs regex")
    print("-" * width)
    bench_text_id()

    print(f"\n[3/6] Post-processing: merged walk vs separate walks")
    print("-" * width)
    bench_post_processing()

    print(f"\n[4/6] Full pipeline: process_data_batch throughput")
    print("-" * width)
    pipeline_results = bench_full_pipeline()

    print(f"\n[5/6] Streaming pipeline: process_data_stream throughput")
    print("-" * width)
    bench_stream()

    print(f"\n[6/6] Memory usage: peak RSS during large batch")
    print("-" * width)
    bench_memory()

    # Summary bar chart
    print(f"\n{'=' * width}")
    print("  Summary: throughput at batch_size=200")
    print(f"{'=' * width}")
    for batch_size, rate in pipeline_results:
        if batch_size <= 200:
            _print_bar(f"batch={batch_size}", rate, max(r for _, r in pipeline_results))

    print(f"\n  FHIRPath speedup: {fast_rate/slow_rate:.0f}x (dict traversal vs fhirpathpy)")
    print(f"  Peak RSS: {_get_rss_mb():.0f} MB")
    print(f"\n{'=' * width}")
    print("  Done.")


if __name__ == "__main__":
    main()
