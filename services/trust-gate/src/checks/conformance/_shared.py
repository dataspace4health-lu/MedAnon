"""Shared infrastructure for the conformance check package.

Holds the module-level state the conformance checks share: tuning constants, the
Prometheus timeout counters, the persistent off-critical-path thread pool, the
compiled FHIR primitive regexes, and the resource traversal iterators. Kept in one
place so each check module (presence/validation/terminology/references) imports its
infra from here rather than re-declaring it.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from prometheus_client import Counter

from constants import FHIR_DATETIME_PATTERN, FHIR_ID_PATTERN, TEMPORAL_FIELD_NAMES

_log = logging.getLogger("trust_gate.checks.conformance")

_DATETIME_RE = re.compile(FHIR_DATETIME_PATTERN)
_ID_RE = re.compile(FHIR_ID_PATTERN)

_TERMINOLOGY_TIMEOUT_SEC = float(
    os.environ.get("TRUST_GATE_TERMINOLOGY_ASYNC_TIMEOUT_SEC", "5")
)
# Validator is the slowest upstream (Java FHIR validator cold start). Bound it on
# the critical path too  a larger default than terminology since validation of a
# whole batch legitimately takes longer than a single code lookup.
_VALIDATOR_TIMEOUT_SEC = float(
    os.environ.get("TRUST_GATE_VALIDATOR_ASYNC_TIMEOUT_SEC", "45")
)
# Structural/profile validation validates EVERY resource (RC4): sampling the only
# BLOCK-gating checks let a malformed resource past the sample window escape. To
# keep a full sweep off the timeout wall, the per-resource validator calls fan out
# across a bounded thread pool and the timeout scales with batch size.
#   _VALIDATOR_CONCURRENCY       parallel validator calls (fan-out width).
#   _VALIDATOR_PER_RESOURCE_SEC  time budget added per resource (timeout scaling).
#   _VALIDATOR_MAX_RESOURCES     safety valve; 0 = unbounded.
_VALIDATOR_CONCURRENCY = int(os.environ.get("TRUST_GATE_VALIDATOR_CONCURRENCY", "8"))
_VALIDATOR_PER_RESOURCE_SEC = float(
    os.environ.get("TRUST_GATE_VALIDATOR_PER_RESOURCE_SEC", "0.5")
)
_VALIDATOR_MAX_RESOURCES = int(
    os.environ.get("TRUST_GATE_VALIDATOR_MAX_RESOURCES", "0")
)
# Retained for the opt-in, non-critical IG-profile check, which still spot-checks
# per type (it is not a BLOCK gate). 0/unset keeps the historical default of 10.
_VALIDATOR_SAMPLE_PER_TYPE = int(
    os.environ.get("TRUST_GATE_VALIDATOR_SAMPLE_PER_TYPE", "10")
)

# Prometheus: incremented whenever an off-path check exceeds its timeout and
# degrades to SKIPPED (NA). Registered in the global default registry so main's
# /metrics endpoint exports it without an import cycle.
TERMINOLOGY_TIMEOUTS = Counter(
    "trust_gate_terminology_timeout_total",
    "Times the terminology check timed out and degraded to SKIPPED (NA)",
)
VALIDATOR_TIMEOUTS = Counter(
    "trust_gate_validator_timeout_total",
    "Times the structural/profile validation timed out and degraded to SKIPPED (NA)",
)

# Persistent worker pool for off-critical-path terminology (and validator)
# evaluation. Created once at module scope  NOT per call. A per-call
# `with ThreadPoolExecutor(...)` block triggers an implicit shutdown(wait=True) on
# exit, which blocks the caller until the slow upstream finishes and defeats the
# entire purpose of the timeout. With a persistent pool, future.result(timeout=...)
# returns immediately on timeout while the abandoned task drains in the background.
_OFFPATH_POOL_SIZE = int(os.environ.get("TRUST_GATE_OFFPATH_POOL_SIZE", "4"))
_offpath_pool = ThreadPoolExecutor(
    max_workers=_OFFPATH_POOL_SIZE, thread_name_prefix="tg-offpath"
)


def _iter_codings(resource: dict):
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            coding = node.get("coding")
            if isinstance(coding, list):
                for c in coding:
                    if isinstance(c, dict):
                        yield c
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)


def _iter_references(resource: dict):
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ref = node.get("reference")
            if isinstance(ref, str):
                yield ref
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)


def _iter_temporal_values(resource: dict):
    """Yield (field_name, value) for every temporal primitive in the resource."""
    stack = [resource]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, val in node.items():
                if key in TEMPORAL_FIELD_NAMES:
                    yield key, val
                elif key == "period" and isinstance(val, dict):
                    for edge in ("start", "end"):
                        if edge in val:
                            yield f"period.{edge}", val[edge]
                if isinstance(val, (dict, list)):
                    stack.append(val)
        elif isinstance(node, list):
            stack.extend(node)
