"""Passport / check-result repository for the Trust Gate platform.

Two backends behind one Protocol (mirrors ``baseline.py``):
  - ``SqlitePassportStore``   — single-instance / dev (WAL, process-local lock).
  - ``PostgresPassportStore`` — durable, cross-replica (use with ``--scale``).

Schema (``trust_gate`` schema on Postgres; plain tables on SQLite):
  - ``assessments``  — one row per Quality Passport (summary columns + full JSON).
  - ``check_results`` — one row per atomic check (for per-metric trend / findings).

PHI-safe: only the passport's own (already-redacted) content is stored; the gate
never persists raw values (see ``passport.redact_token`` / ``accuracy.py``).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Protocol

_log = logging.getLogger("trust_gate.store")

# Stable namespace for deriving a deterministic assessment id from a caller's
# idempotency key, so an at-least-once retry (or a racing replica) collapses onto
# one row instead of duplicating the assessment.
_IDEMPOTENCY_NS = uuid.UUID("6f9b1d2a-0c3e-4a7b-9c11-7e2f5d8a4c10")


def _derive_assessment_id(idempotency_key: str) -> str:
    """Deterministic assessment id for an idempotency key (uuid5, stable)."""
    return uuid.uuid5(_IDEMPOTENCY_NS, idempotency_key).hex

# Append-only ALCOA++ audit columns (attributable / contemporaneous / traceable /
# enduring): one immutable row per assessment, recorded at persistence time.
_AUDIT_COLS = (
    "id", "assessment_id", "dataset_id", "provider_id", "decision",
    "generated_at", "recorded_at", "source_model",
)


def _audit_row(passport: dict, assessment_id: str, provider_id: str | None) -> tuple:
    return (
        uuid.uuid4().hex,
        assessment_id,
        str(passport.get("dataset_id", "dataset")),
        provider_id,
        str(passport.get("decision", "")),
        str(passport.get("generated_at", "")),
        datetime.now(timezone.utc).isoformat(),
        str((passport.get("evaluation") or {}).get("source_model", "fhir")),
    )


def _summary_from_passport(passport: dict) -> dict:
    """Extract the indexed summary columns from a full passport dict."""
    return {
        "decision": str(passport.get("decision", "")),
        "overall_score": float(passport.get("overall_score") or 0.0),
        "overall_grade": passport.get("overall_grade"),
        "dataset_id": str(passport.get("dataset_id", "dataset")),
        "generated_at": str(passport.get("generated_at", "")),
    }


def _check_rows(assessment_id: str, passport: dict) -> list[tuple]:
    """Flatten passport checks into ``check_results`` rows."""
    rows: list[tuple] = []
    for c in passport.get("checks", []) or []:
        if not isinstance(c, dict):
            continue
        rows.append(
            (
                assessment_id,
                str(c.get("check_id", "")),
                str(c.get("category", "")),
                str(c.get("dimension", "")),
                str(c.get("result", "")),
                float(c.get("violation_fraction") or 0.0),
            )
        )
    return rows


def _trend_point(passport: dict) -> dict:
    """One time-series point: overall verdict + per-dimension scores."""
    scorecard = passport.get("scorecard") or {}
    dimensions = {
        dim: entry.get("score")
        for dim, entry in scorecard.items()
        if isinstance(entry, dict)
    }
    return {
        "generated_at": str(passport.get("generated_at", "")),
        "decision": str(passport.get("decision", "")),
        "overall_score": passport.get("overall_score"),
        "overall_grade": passport.get("overall_grade"),
        "dimensions": dimensions,
    }


class PassportStore(Protocol):
    def save(
        self,
        passport: dict,
        *,
        provider_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str: ...
    def list_assessments(self, provider_id: str, limit: int = 50) -> list[dict]: ...
    def history(self, dataset_id: str, limit: int = 50) -> list[dict]: ...
    def trend(self, dataset_id: str, limit: int = 100) -> list[dict]: ...
    def get(self, assessment_id: str) -> dict | None: ...
    def audit(self, dataset_id: str, limit: int = 100) -> list[dict]: ...


_SUMMARY_COLS = (
    "id",
    "provider_id",
    "dataset_id",
    "decision",
    "overall_score",
    "overall_grade",
    "generated_at",
)


class SqlitePassportStore:
    """Single-instance durable store. Use Postgres for cross-replica scaling."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assessments (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT,
                    dataset_id TEXT,
                    decision TEXT,
                    overall_score REAL,
                    overall_grade TEXT,
                    generated_at TEXT,
                    passport TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS check_results (
                    assessment_id TEXT NOT NULL,
                    check_id TEXT,
                    category TEXT,
                    dimension TEXT,
                    result TEXT,
                    violation_fraction REAL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_assessments_provider "
                "ON assessments(provider_id, generated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_assessments_dataset "
                "ON assessments(dataset_id, generated_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_check_results_assessment "
                "ON check_results(assessment_id)"
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS audit_log (
                    {', '.join(f'{c} TEXT' for c in _AUDIT_COLS)}
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_audit_dataset "
                "ON audit_log(dataset_id, recorded_at)"
            )

    def save(
        self,
        passport: dict,
        *,
        provider_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        # A supplied key derives a stable id; a retry/replica race then collapses
        # onto the same row via INSERT OR IGNORE (no duplicate child rows either).
        aid = _derive_assessment_id(idempotency_key) if idempotency_key else uuid.uuid4().hex
        verb = "INSERT OR IGNORE INTO" if idempotency_key else "INSERT INTO"
        s = _summary_from_passport(passport)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"{verb} assessments "
                "(id, provider_id, dataset_id, decision, overall_score, "
                "overall_grade, generated_at, passport) VALUES (?,?,?,?,?,?,?,?)",
                (
                    aid,
                    provider_id,
                    s["dataset_id"],
                    s["decision"],
                    s["overall_score"],
                    s["overall_grade"],
                    s["generated_at"],
                    json.dumps(passport),
                ),
            )
            if idempotency_key and cur.rowcount == 0:
                return aid  # already persisted under this key — idempotent replay
            rows = _check_rows(aid, passport)
            if rows:
                conn.executemany(
                    "INSERT INTO check_results "
                    "(assessment_id, check_id, category, dimension, result, "
                    "violation_fraction) VALUES (?,?,?,?,?,?)",
                    rows,
                )
            conn.execute(
                f"INSERT INTO audit_log ({', '.join(_AUDIT_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _AUDIT_COLS)})",
                _audit_row(passport, aid, provider_id),
            )
        return aid

    def audit(self, dataset_id: str, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {', '.join(_AUDIT_COLS)} FROM audit_log WHERE dataset_id = ? "
                f"ORDER BY recorded_at DESC LIMIT ?",
                (dataset_id, limit),
            )
            return [dict(zip(_AUDIT_COLS, row)) for row in cur.fetchall()]

    def _summaries(self, where: str, params: tuple, limit: int) -> list[dict]:
        sql = (
            f"SELECT {', '.join(_SUMMARY_COLS)} FROM assessments "
            f"WHERE {where} ORDER BY generated_at DESC LIMIT ?"
        )
        with self._connect() as conn:
            cur = conn.execute(sql, (*params, limit))
            return [dict(zip(_SUMMARY_COLS, row)) for row in cur.fetchall()]

    def list_assessments(self, provider_id: str, limit: int = 50) -> list[dict]:
        return self._summaries("provider_id = ?", (provider_id,), limit)

    def history(self, dataset_id: str, limit: int = 50) -> list[dict]:
        return self._summaries("dataset_id = ?", (dataset_id,), limit)

    def trend(self, dataset_id: str, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT passport FROM assessments WHERE dataset_id = ? "
                "ORDER BY generated_at ASC LIMIT ?",
                (dataset_id, limit),
            )
            return [_trend_point(json.loads(row[0])) for row in cur.fetchall()]

    def get(self, assessment_id: str) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT passport FROM assessments WHERE id = ?", (assessment_id,)
            )
            row = cur.fetchone()
            return json.loads(row[0]) if row else None


class PostgresPassportStore:
    """Durable, cross-replica store in the ``trust_gate`` schema."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure_schema()

    def _connect(self):
        import psycopg2  # lazy: only needed when a store DB is configured

        return psycopg2.connect(self._dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS trust_gate")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_gate.assessments (
                    id text PRIMARY KEY,
                    provider_id text,
                    dataset_id text,
                    decision text,
                    overall_score double precision,
                    overall_grade text,
                    generated_at text,
                    passport jsonb NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_gate.check_results (
                    assessment_id text NOT NULL,
                    check_id text,
                    category text,
                    dimension text,
                    result text,
                    violation_fraction double precision
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_assessments_provider "
                "ON trust_gate.assessments(provider_id, generated_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_assessments_dataset "
                "ON trust_gate.assessments(dataset_id, generated_at)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_check_results_assessment "
                "ON trust_gate.check_results(assessment_id)"
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS trust_gate.audit_log (
                    {', '.join(f'{c} text' for c in _AUDIT_COLS)}
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_audit_dataset "
                "ON trust_gate.audit_log(dataset_id, recorded_at)"
            )
            conn.commit()

    def save(
        self,
        passport: dict,
        *,
        provider_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        # ON CONFLICT (id) DO NOTHING makes a concurrent retry/replica race
        # idempotent at the database (the assessments PK is the dedup point); on a
        # conflict no child rows are written and the existing id is returned.
        aid = _derive_assessment_id(idempotency_key) if idempotency_key else uuid.uuid4().hex
        conflict = " ON CONFLICT (id) DO NOTHING" if idempotency_key else ""
        s = _summary_from_passport(passport)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO trust_gate.assessments "
                "(id, provider_id, dataset_id, decision, overall_score, "
                "overall_grade, generated_at, passport) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s)" + conflict,
                (
                    aid,
                    provider_id,
                    s["dataset_id"],
                    s["decision"],
                    s["overall_score"],
                    s["overall_grade"],
                    s["generated_at"],
                    json.dumps(passport),
                ),
            )
            if idempotency_key and cur.rowcount == 0:
                conn.commit()  # already persisted under this key — idempotent replay
                return aid
            rows = _check_rows(aid, passport)
            if rows:
                cur.executemany(
                    "INSERT INTO trust_gate.check_results "
                    "(assessment_id, check_id, category, dimension, result, "
                    "violation_fraction) VALUES (%s,%s,%s,%s,%s,%s)",
                    rows,
                )
            cur.execute(
                f"INSERT INTO trust_gate.audit_log ({', '.join(_AUDIT_COLS)}) "
                f"VALUES ({', '.join('%s' for _ in _AUDIT_COLS)})",
                _audit_row(passport, aid, provider_id),
            )
            conn.commit()
        return aid

    def audit(self, dataset_id: str, limit: int = 100) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_AUDIT_COLS)} FROM trust_gate.audit_log "
                f"WHERE dataset_id = %s ORDER BY recorded_at DESC LIMIT %s",
                (dataset_id, limit),
            )
            return [dict(zip(_AUDIT_COLS, row)) for row in cur.fetchall()]

    def _summaries(self, where: str, params: tuple, limit: int) -> list[dict]:
        sql = (
            f"SELECT {', '.join(_SUMMARY_COLS)} FROM trust_gate.assessments "
            f"WHERE {where} ORDER BY generated_at DESC LIMIT %s"
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, (*params, limit))
            return [dict(zip(_SUMMARY_COLS, row)) for row in cur.fetchall()]

    def list_assessments(self, provider_id: str, limit: int = 50) -> list[dict]:
        return self._summaries("provider_id = %s", (provider_id,), limit)

    def history(self, dataset_id: str, limit: int = 50) -> list[dict]:
        return self._summaries("dataset_id = %s", (dataset_id,), limit)

    def trend(self, dataset_id: str, limit: int = 100) -> list[dict]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT passport FROM trust_gate.assessments WHERE dataset_id = %s "
                "ORDER BY generated_at ASC LIMIT %s",
                (dataset_id, limit),
            )
            return [_trend_point(row[0]) for row in cur.fetchall()]

    def get(self, assessment_id: str) -> dict | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT passport FROM trust_gate.assessments WHERE id = %s",
                (assessment_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None


_STORE: PassportStore | None = None
_STORE_INIT = False


def get_passport_store() -> PassportStore | None:
    """Singleton passport store per env config (None = stateless)."""
    global _STORE, _STORE_INIT
    if _STORE_INIT:
        return _STORE
    _STORE_INIT = True
    dsn = os.environ.get("TRUST_GATE_STORE_DB_URL", "").strip()
    sqlite_path = os.environ.get("TRUST_GATE_STORE_DB", "").strip()
    try:
        if dsn:
            _STORE = PostgresPassportStore(dsn)
            _log.info("trust-gate store: Postgres (durable, cross-replica)")
        elif sqlite_path:
            _STORE = SqlitePassportStore(sqlite_path)
            _log.info("trust-gate store: SQLite at %s", sqlite_path)
        else:
            _STORE = None  # stateless: assessments are not retained
    except Exception as exc:  # noqa: BLE001 — never let store setup break the gate
        _log.warning("passport store init failed (%s) — running stateless", exc)
        _STORE = None
    return _STORE


def reset_passport_store() -> None:
    """Test hook: force re-selection of the store on next access."""
    global _STORE, _STORE_INIT
    _STORE = None
    _STORE_INIT = False
