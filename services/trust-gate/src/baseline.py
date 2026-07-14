"""Persisted cross-batch value baselines for outlier detection.

The batch-relative outlier check is blind to two things: small batches (no
distribution to learn) and *whole-batch* corruption (a uniformly wrong feed looks
internally clean). A persisted baseline fixes both — each batch is judged against
a distribution accumulated across *previous* batches for the same (code, unit).

We keep a bounded **reservoir sample** per (code, unit) (Vitter's Algorithm R) so
storage stays O(reservoir_size) regardless of volume while remaining an unbiased
sample of the population — enough to compute a stable median/MAD/IQR.

Backends (selected by ``get_baseline_store``):
  - none (default)          — stateless; outlier check uses batch-only (current).
  - ``TRUST_GATE_BASELINE=memory`` — in-process dict (single-process / dev / tests).
  - ``TRUST_GATE_BASELINE_DB_URL`` — Postgres, durable + cross-replica.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from typing import Protocol

from constants import BASELINE_RESERVOIR_SIZE, SAMPLER_SEED

_log = logging.getLogger("trust_gate.baseline")

# Seeded RNG so reservoir sampling is reproducible (Phase 1.5 determinism): two
# runs over the same insertion order yield the same retained sample, so the
# statistical advisory is deterministic given a pinned baseline.
_RNG = random.Random(SAMPLER_SEED)


def _reservoir_merge(
    reservoir: list[float], n_seen: int, new: list[float], cap: int
) -> tuple[list[float], int]:
    """Add *new* values to a reservoir sample of size <= cap (Algorithm R)."""
    res = list(reservoir)
    n = n_seen
    for v in new:
        n += 1
        if len(res) < cap:
            res.append(v)
        else:
            j = _RNG.randint(0, n - 1)  # noqa: S311 — sampling, not security
            if j < cap:
                res[j] = v
    return res, n


class BaselineStore(Protocol):
    def get(self, code: str, unit: str) -> list[float]: ...
    def update(self, code: str, unit: str, values: list[float]) -> None: ...


class InMemoryBaselineStore:
    """Process-local baseline. Not shared across uvicorn workers/replicas — use
    the Postgres backend for durable, consistent cross-replica baselines."""

    def __init__(self, cap: int = BASELINE_RESERVOIR_SIZE) -> None:
        self._cap = cap
        self._data: dict[tuple[str, str], tuple[list[float], int]] = {}
        self._lock = threading.Lock()

    def get(self, code: str, unit: str) -> list[float]:
        with self._lock:
            return list(self._data.get((code, unit), ([], 0))[0])

    def update(self, code: str, unit: str, values: list[float]) -> None:
        if not values:
            return
        with self._lock:
            res, n = self._data.get((code, unit), ([], 0))
            self._data[(code, unit)] = _reservoir_merge(res, n, values, self._cap)


class PostgresBaselineStore:
    """Durable, cross-replica baseline in ``trust_gate.value_baselines``."""

    def __init__(self, dsn: str, cap: int = BASELINE_RESERVOIR_SIZE) -> None:
        self._dsn = dsn
        self._cap = cap
        self._ensure_schema()

    def _connect(self):
        import psycopg2  # lazy: only needed when a baseline DB is configured

        return psycopg2.connect(self._dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS trust_gate")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_gate.value_baselines (
                    code text NOT NULL,
                    unit text NOT NULL,
                    n bigint NOT NULL DEFAULT 0,
                    reservoir jsonb NOT NULL DEFAULT '[]'::jsonb,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    PRIMARY KEY (code, unit)
                )
                """
            )
            conn.commit()

    def get(self, code: str, unit: str) -> list[float]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reservoir FROM trust_gate.value_baselines WHERE code=%s AND unit=%s",
                (code, unit),
            )
            row = cur.fetchone()
            return [float(x) for x in (row[0] if row else [])]

    def update(self, code: str, unit: str, values: list[float]) -> None:
        if not values:
            return
        import json

        with self._connect() as conn, conn.cursor() as cur:
            # Lock the row (or create it) so concurrent batches merge safely.
            cur.execute(
                """
                INSERT INTO trust_gate.value_baselines (code, unit)
                VALUES (%s, %s) ON CONFLICT (code, unit) DO NOTHING
                """,
                (code, unit),
            )
            cur.execute(
                "SELECT n, reservoir FROM trust_gate.value_baselines "
                "WHERE code=%s AND unit=%s FOR UPDATE",
                (code, unit),
            )
            n_seen, reservoir = cur.fetchone()
            res, n = _reservoir_merge(
                [float(x) for x in reservoir], int(n_seen), values, self._cap
            )
            cur.execute(
                "UPDATE trust_gate.value_baselines SET n=%s, reservoir=%s, "
                "updated_at=now() WHERE code=%s AND unit=%s",
                (n, json.dumps(res), code, unit),
            )
            conn.commit()


_STORE: BaselineStore | None = None
_STORE_INIT = False


def get_baseline_store() -> BaselineStore | None:
    """Singleton baseline store per the env configuration (None = stateless)."""
    global _STORE, _STORE_INIT
    if _STORE_INIT:
        return _STORE
    _STORE_INIT = True
    dsn = os.environ.get("TRUST_GATE_BASELINE_DB_URL", "").strip()
    mode = os.environ.get("TRUST_GATE_BASELINE", "").strip().lower()
    try:
        if dsn:
            _STORE = PostgresBaselineStore(dsn)
            _log.info("trust-gate baseline: Postgres (durable, cross-replica)")
        elif mode == "memory":
            _STORE = InMemoryBaselineStore()
            _log.info("trust-gate baseline: in-memory (process-local)")
        else:
            _STORE = None  # stateless: outlier check is batch-only
    except Exception as exc:  # noqa: BLE001 — never let baseline setup break the gate
        _log.warning("baseline store init failed (%s) — running stateless", exc)
        _STORE = None
    return _STORE
