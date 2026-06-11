"""SQL source service — saved connections, schema inspection, export submit.

Connection passwords are encrypted before they reach the store and decrypted only
to open a (read-only, allow-list-guarded) connection.  Reflection runs in a worker
thread so the event loop is never blocked on blocking DB I/O.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("medanon")


class SqlConnectionNotFound(Exception):
    """Raised when a referenced connection id does not exist."""


class SqlStoreUnavailable(Exception):
    """Raised when the connection store is not initialised (no app DB)."""


def _store():
    from pipeline.sql_connection import get_sql_connection_store

    store = get_sql_connection_store()
    if store is None:
        raise SqlStoreUnavailable(
            "SQL connection store is not initialised — set MEDANON_APP_DB_URL "
            "(PostgreSQL) to enable saved SQL source connections."
        )
    return store


class SqlSourceService:
    """Manage saved SQL connections and inspect/export their schemas."""

    # -- connection CRUD ------------------------------------------------------

    def create_connection(
        self, *, name, host, port, dbname, username, password, sslmode="prefer"
    ) -> dict:
        """Validate the host, encrypt the password, and persist the connection."""
        from integrations.sql_source import assert_host_allowed
        from integrations.sql_source.secrets import encrypt_secret

        assert_host_allowed(host)  # fail fast before storing an unreachable host
        return _store().create(
            name=name,
            host=host,
            port=port,
            dbname=dbname,
            username=username,
            password_enc=encrypt_secret(password),
            sslmode=sslmode,
        )

    def list_connections(self) -> list[dict]:
        return _store().list_all()

    def delete_connection(self, conn_id: str) -> bool:
        return _store().delete(conn_id)

    # -- connection use (blocking I/O → worker thread) ------------------------

    def _conn_info(self, conn_id: str) -> dict:
        from integrations.sql_source.secrets import decrypt_secret

        store = _store()
        meta = store.get(conn_id)
        if meta is None:
            raise SqlConnectionNotFound(conn_id)
        enc = store.get_encrypted_password(conn_id)
        return {
            "host": meta["host"],
            "port": meta["port"],
            "dbname": meta["dbname"],
            "username": meta["username"],
            "sslmode": meta.get("sslmode", "prefer"),
            "password": decrypt_secret(enc) if enc else "",
        }

    async def test_connection(self, conn_id: str) -> dict:
        """Open and immediately close a read-only connection. Returns {ok, server}."""
        return await asyncio.to_thread(self._test_connection_sync, conn_id)

    def _test_connection_sync(self, conn_id: str) -> dict:
        from contextlib import closing

        from integrations.sql_source import open_readonly_connection

        info = self._conn_info(conn_id)
        with closing(open_readonly_connection(info)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
                version = row[0] if not isinstance(row, dict) else row.get("version")
        return {"ok": True, "server": version}

    async def inspect(self, conn_id: str, schema: str = "public") -> dict:
        """Reflect the schema: tables × columns × samples × recommended_action."""
        return await asyncio.to_thread(self._inspect_sync, conn_id, schema)

    def _inspect_sync(self, conn_id: str, schema: str) -> dict:
        from contextlib import closing

        from integrations.sql_source import (
            describe_table,
            list_tables,
            open_readonly_connection,
        )
        from pipeline.sources import recommend_column_action

        info = self._conn_info(conn_id)
        with closing(open_readonly_connection(info)) as conn:
            tables = list_tables(conn, schema)
            out_tables = []
            for tbl in tables:
                cols = describe_table(conn, schema, tbl["name"])
                out_tables.append(
                    {
                        "name": tbl["name"],
                        "row_estimate": tbl["row_estimate"],
                        "columns": [
                            {
                                "name": c["name"],
                                "data_type": c["data_type"],
                                "samples": c["samples"],
                                "recommended_action": recommend_column_action(
                                    c["name"], c["samples"]
                                ),
                            }
                            for c in cols
                        ],
                    }
                )
        return {"schema": schema, "tables": out_tables}
