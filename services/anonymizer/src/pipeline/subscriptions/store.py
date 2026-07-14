"""FHIR R4 Subscription persistence  SQLite-backed store.

Stores active Subscription resources. Used by the subscription dispatcher
to find matching subscriptions after resource processing.

Only 'rest-hook' channel type is supported (webhook delivery).
"""

import json
import logging
import os
import sqlite3
from contextlib import AbstractContextManager
import uuid
from datetime import datetime, timezone
from pathlib import Path
from utils.sqlite_store import connect as sqlite_connect

_sub_log = logging.getLogger("medanon.subscriptions")
_DEFAULT_SUB_DB = "/output/subscriptions.db"


class SqliteSubscriptionStore:
    """SQLite-backed FHIR R4 Subscription store. Thread-safe via WAL journal mode."""

    def __init__(self, db_path: str | None = None) -> None:
        self._path = db_path or os.environ.get(
            "MEDANON_SUBSCRIPTION_DB", _DEFAULT_SUB_DB
        )
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> "AbstractContextManager[sqlite3.Connection]":
        """WAL connection, committed and **closed** on exit. See utils.sqlite_store."""
        return sqlite_connect(self._path)

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id           TEXT PRIMARY KEY,
                    status       TEXT NOT NULL,
                    criteria     TEXT NOT NULL,
                    channel_type TEXT NOT NULL,
                    endpoint     TEXT NOT NULL,
                    headers      TEXT NOT NULL DEFAULT '[]',
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL,
                    resource     TEXT NOT NULL
                )
            """)

    def create(self, sub: dict) -> dict:
        """Persist a new subscription. Generates id if not present. Returns saved sub."""
        sub_copy = dict(sub)
        if not sub_copy.get("id"):
            sub_copy["id"] = str(uuid.uuid4())
        sub_copy["resourceType"] = "Subscription"
        sub_copy.setdefault("status", "active")
        now = datetime.now(timezone.utc).isoformat()
        sub_copy.setdefault("meta", {})["lastUpdated"] = now

        channel = sub_copy.get("channel", {})
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO subscriptions VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    sub_copy["id"],
                    sub_copy.get("status", "active"),
                    sub_copy.get("criteria", ""),
                    channel.get("type", ""),
                    channel.get("endpoint", ""),
                    json.dumps(channel.get("header", [])),
                    now,
                    now,
                    json.dumps(sub_copy),
                ),
            )
        _sub_log.info(
            "subscription_created id=%s criteria=%s",
            sub_copy["id"],
            sub_copy.get("criteria"),
        )
        return sub_copy

    def get(self, sub_id: str) -> dict | None:
        """Return a subscription by id, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT resource FROM subscriptions WHERE id=?", (sub_id,)
            ).fetchone()
        return json.loads(row["resource"]) if row else None

    def update(self, sub_id: str, sub: dict) -> dict | None:
        """Replace a subscription by id. Returns None if not found."""
        existing = self.get(sub_id)
        if existing is None:
            return None
        sub_copy = dict(sub)
        sub_copy["id"] = sub_id
        now = datetime.now(timezone.utc).isoformat()
        sub_copy.setdefault("meta", {})["lastUpdated"] = now
        channel = sub_copy.get("channel", {})
        with self._connect() as conn:
            conn.execute(
                """UPDATE subscriptions
                   SET status=?, criteria=?, channel_type=?, endpoint=?,
                       headers=?, updated_at=?, resource=?
                   WHERE id=?""",
                (
                    sub_copy.get("status", "active"),
                    sub_copy.get("criteria", ""),
                    channel.get("type", ""),
                    channel.get("endpoint", ""),
                    json.dumps(channel.get("header", [])),
                    now,
                    json.dumps(sub_copy),
                    sub_id,
                ),
            )
        return sub_copy

    def delete(self, sub_id: str) -> bool:
        """Delete a subscription by id. Returns True if a row was removed."""
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
        return cursor.rowcount > 0

    def list_active(self) -> list[dict]:
        """Return all subscriptions with status='active'."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT resource FROM subscriptions WHERE status='active'"
            ).fetchall()
        return [json.loads(row["resource"]) for row in rows]

    def list_all(self) -> list[dict]:
        """Return all subscriptions ordered by creation time descending."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT resource FROM subscriptions ORDER BY created_at DESC"
            ).fetchall()
        return [json.loads(row["resource"]) for row in rows]
