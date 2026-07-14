"""Remediation findings store + derivation (Phase 5, PDSA loop).

A *finding* is an open data-quality issue raised by a failing check, tracked
through a lifecycle (open -> triaged -> resolved) with a root-cause classification
(source-error / etl-error / genuine-biology / unknown) and an owner. This is the
"study / act" half of the PDSA loop the literature requires: the gate does not
just report quality, it tracks remediation over time.

Backends mirror ``passport_store`` and select on the same env (Postgres via
``TRUST_GATE_STORE_DB_URL``, else SQLite via ``TRUST_GATE_STORE_DB``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Protocol

_log = logging.getLogger("trust_gate.findings")

STATUSES = ("open", "triaged", "resolved")
ROOT_CAUSES = ("unknown", "source_error", "etl_error", "genuine_biology")

_COLS = (
    "id",
    "assessment_id",
    "dataset_id",
    "check_id",
    "severity",
    "status",
    "root_cause",
    "owner",
    "note",
    "created_at",
    "updated_at",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_findings(passport: dict, assessment_id: str) -> list[dict]:
    """Open findings from a passport's failing, verdict-driving checks.

    Advisory (statistical) checks do not raise findings — they are not defects,
    just signals. Critical failures are severity ``critical``; others ``major``.
    """
    dataset_id = str(passport.get("dataset_id", "dataset"))
    findings: list[dict] = []
    for c in passport.get("checks", []) or []:
        if not isinstance(c, dict) or c.get("advisory"):
            continue
        if c.get("result") != "FAIL":
            continue
        findings.append(
            {
                "assessment_id": assessment_id,
                "dataset_id": dataset_id,
                "check_id": c.get("check_id", ""),
                "severity": "critical" if c.get("critical") else "major",
                "status": "open",
                "root_cause": "unknown",
                "owner": None,
                "note": c.get("recommendation", ""),
            }
        )
    return findings


class FindingsStore(Protocol):
    def create(self, finding: dict) -> str: ...
    def get(self, finding_id: str) -> dict | None: ...
    def list(
        self,
        *,
        dataset_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]: ...
    def transition(self, finding_id: str, **fields) -> dict | None: ...


def _normalize(finding: dict, fid: str) -> dict:
    now = _now()
    return {
        "id": fid,
        "assessment_id": finding.get("assessment_id"),
        "dataset_id": finding.get("dataset_id"),
        "check_id": finding.get("check_id"),
        "severity": finding.get("severity", "major"),
        "status": finding.get("status", "open"),
        "root_cause": finding.get("root_cause", "unknown"),
        "owner": finding.get("owner"),
        "note": finding.get("note", ""),
        "created_at": finding.get("created_at", now),
        "updated_at": now,
    }


class SqliteFindingsStore:
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
                CREATE TABLE IF NOT EXISTS findings (
                    id TEXT PRIMARY KEY,
                    assessment_id TEXT,
                    dataset_id TEXT,
                    check_id TEXT,
                    severity TEXT,
                    status TEXT,
                    root_cause TEXT,
                    owner TEXT,
                    note TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_findings_dataset "
                "ON findings(dataset_id, status)"
            )

    def create(self, finding: dict) -> str:
        rec = _normalize(finding, uuid.uuid4().hex)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO findings ({', '.join(_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _COLS)})",
                tuple(rec[c] for c in _COLS),
            )
        return rec["id"]

    def get(self, finding_id: str) -> dict | None:
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {', '.join(_COLS)} FROM findings WHERE id = ?", (finding_id,)
            )
            row = cur.fetchone()
            return dict(zip(_COLS, row)) if row else None

    def list(self, *, dataset_id=None, status=None, limit=100) -> list[dict]:
        clauses, params = [], []
        if dataset_id:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT {', '.join(_COLS)} FROM findings {where} "
                f"ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            )
            return [dict(zip(_COLS, row)) for row in cur.fetchall()]

    def transition(self, finding_id: str, **fields) -> dict | None:
        current = self.get(finding_id)
        if current is None:
            return None
        for key in ("status", "root_cause", "owner", "note"):
            if fields.get(key) is not None:
                current[key] = fields[key]
        current["updated_at"] = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE findings SET status=?, root_cause=?, owner=?, note=?, "
                "updated_at=? WHERE id=?",
                (
                    current["status"],
                    current["root_cause"],
                    current["owner"],
                    current["note"],
                    current["updated_at"],
                    finding_id,
                ),
            )
        return current


class PostgresFindingsStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._ensure_schema()

    def _connect(self):
        import psycopg2

        return psycopg2.connect(self._dsn)

    def _ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS trust_gate")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS trust_gate.findings (
                    id text PRIMARY KEY,
                    assessment_id text,
                    dataset_id text,
                    check_id text,
                    severity text,
                    status text,
                    root_cause text,
                    owner text,
                    note text,
                    created_at text,
                    updated_at text
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS ix_findings_dataset "
                "ON trust_gate.findings(dataset_id, status)"
            )
            conn.commit()

    def create(self, finding: dict) -> str:
        rec = _normalize(finding, uuid.uuid4().hex)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO trust_gate.findings ({', '.join(_COLS)}) "
                f"VALUES ({', '.join('%s' for _ in _COLS)})",
                tuple(rec[c] for c in _COLS),
            )
            conn.commit()
        return rec["id"]

    def get(self, finding_id: str) -> dict | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLS)} FROM trust_gate.findings WHERE id = %s",
                (finding_id,),
            )
            row = cur.fetchone()
            return dict(zip(_COLS, row)) if row else None

    def list(self, *, dataset_id=None, status=None, limit=100) -> list[dict]:
        clauses, params = [], []
        if dataset_id:
            clauses.append("dataset_id = %s")
            params.append(dataset_id)
        if status:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(_COLS)} FROM trust_gate.findings {where} "
                f"ORDER BY created_at DESC LIMIT %s",
                (*params, limit),
            )
            return [dict(zip(_COLS, row)) for row in cur.fetchall()]

    def transition(self, finding_id: str, **fields) -> dict | None:
        current = self.get(finding_id)
        if current is None:
            return None
        for key in ("status", "root_cause", "owner", "note"):
            if fields.get(key) is not None:
                current[key] = fields[key]
        current["updated_at"] = _now()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE trust_gate.findings SET status=%s, root_cause=%s, owner=%s, "
                "note=%s, updated_at=%s WHERE id=%s",
                (
                    current["status"],
                    current["root_cause"],
                    current["owner"],
                    current["note"],
                    current["updated_at"],
                    finding_id,
                ),
            )
            conn.commit()
        return current


_STORE: FindingsStore | None = None
_STORE_INIT = False


def get_findings_store() -> FindingsStore | None:
    global _STORE, _STORE_INIT
    if _STORE_INIT:
        return _STORE
    _STORE_INIT = True
    dsn = os.environ.get("TRUST_GATE_STORE_DB_URL", "").strip()
    sqlite_path = os.environ.get("TRUST_GATE_STORE_DB", "").strip()
    try:
        if dsn:
            _STORE = PostgresFindingsStore(dsn)
        elif sqlite_path:
            _STORE = SqliteFindingsStore(sqlite_path)
        else:
            _STORE = None
    except Exception as exc:  # noqa: BLE001
        _log.warning("findings store init failed (%s) — disabled", exc)
        _STORE = None
    return _STORE


def reset_findings_store() -> None:
    global _STORE, _STORE_INIT
    _STORE = None
    _STORE_INIT = False
