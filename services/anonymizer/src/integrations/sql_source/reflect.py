"""PostgreSQL schema reflection + chunked row reader for SQL sources.

All table/column names reach SQL only through ``psycopg2.sql.Identifier`` (never
string interpolation), so a maliciously named object cannot inject SQL.  Reads
are streamed in chunks — by primary key (keyset pagination) when a single-column
PK exists, else by ``OFFSET/LIMIT`` — to keep memory bounded on large tables.
"""

from __future__ import annotations

import logging
from typing import Iterator

from psycopg2 import sql

_log = logging.getLogger("medanon.sql_source")

_DEFAULT_CHUNK = 1000


def list_tables(conn, schema: str = "public") -> list[dict]:
    """Return base tables in *schema* with a cheap row-count estimate.

    Estimate comes from ``pg_class.reltuples`` (planner statistics) — instant even
    on huge tables, unlike ``COUNT(*)``.
    """
    query = """
        SELECT c.relname AS name,
               GREATEST(c.reltuples, 0)::bigint AS row_estimate
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind = 'r'
        ORDER BY c.relname
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema,))
        return [
            {
                "schema": schema,
                "name": r["name"],
                "row_estimate": int(r["row_estimate"]),
            }
            for r in cur.fetchall()
        ]


def primary_key(conn, schema: str, table: str) -> str | None:
    """Return the single-column primary key name, or None (composite/none).

    A single-column PK enables keyset pagination; otherwise the reader falls back
    to OFFSET/LIMIT.
    """
    query = """
        SELECT a.attname AS col
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
        WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary
    """
    with conn.cursor() as cur:
        cur.execute(query, (schema, table))
        rows = cur.fetchall()
    return rows[0]["col"] if len(rows) == 1 else None


def describe_table(conn, schema: str, table: str, sample_rows: int = 5) -> list[dict]:
    """Return ``[{name, data_type, samples}]`` for *table*.

    Column names/types come from ``information_schema.columns``; sample values
    from a ``SELECT … LIMIT n`` (identifiers safely quoted).  Samples are
    stringified + truncated, mirroring ``TabularAdapter.inspect``.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        cols = [(r["column_name"], r["data_type"]) for r in cur.fetchall()]

    samples: dict[str, list[str]] = {name: [] for name, _ in cols}
    if cols and sample_rows > 0:
        stmt = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        with conn.cursor() as cur:
            cur.execute(stmt, (int(sample_rows),))
            for row in cur.fetchall():
                for name, _ in cols:
                    val = row.get(name)
                    if val is None or val == "":
                        continue
                    s = str(val)
                    samples[name].append(s[:80] + "…" if len(s) > 80 else s)

    return [
        {"name": name, "data_type": dtype, "samples": samples[name]}
        for name, dtype in cols
    ]


def iter_rows(
    conn,
    schema: str,
    table: str,
    *,
    chunk_size: int = _DEFAULT_CHUNK,
    pk: str | None = None,
) -> Iterator[list[dict]]:
    """Yield lists of row dicts from *table*, ``chunk_size`` at a time.

    Uses keyset pagination on *pk* when given (stable, index-friendly); otherwise
    OFFSET/LIMIT.  Each yielded row is a plain ``dict`` (column → value).
    """
    qschema, qtable = sql.Identifier(schema), sql.Identifier(table)

    if pk:
        qpk = sql.Identifier(pk)
        last = None
        while True:
            if last is None:
                stmt = sql.SQL("SELECT * FROM {}.{} ORDER BY {} ASC LIMIT %s").format(
                    qschema, qtable, qpk
                )
                params: tuple = (chunk_size,)
            else:
                stmt = sql.SQL(
                    "SELECT * FROM {}.{} WHERE {} > %s ORDER BY {} ASC LIMIT %s"
                ).format(qschema, qtable, qpk, qpk)
                params = (last, chunk_size)
            with conn.cursor() as cur:
                cur.execute(stmt, params)
                rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return
            yield rows
            if len(rows) < chunk_size:
                return
            last = rows[-1][pk]
    else:
        offset = 0
        while True:
            stmt = sql.SQL("SELECT * FROM {}.{} OFFSET %s LIMIT %s").format(
                qschema, qtable
            )
            with conn.cursor() as cur:
                cur.execute(stmt, (offset, chunk_size))
                rows = [dict(r) for r in cur.fetchall()]
            if not rows:
                return
            yield rows
            if len(rows) < chunk_size:
                return
            offset += chunk_size
