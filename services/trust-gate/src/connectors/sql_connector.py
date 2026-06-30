"""SQL connector: connect to PostgreSQL / MySQL / SQLite and run a SELECT query.

Security model
--------------
- Only SELECT statements are accepted (DDL/DML are rejected before connection).
- Host is validated against TRUST_GATE_SQL_ALLOWED_HOSTS (comma-separated).
  Set to "*" only in fully-trusted network environments; leave empty to disable
  the connector entirely.
- Row count is capped at TRUST_GATE_SQL_MAX_ROWS (default 20 000) to prevent
  memory exhaustion.
- Credentials are never logged or stored beyond the lifetime of the request.
- SQLite is allowed unconditionally (local file, no network hop).

Supported drivers
-----------------
  "postgresql"  — psycopg2-binary (bundled in requirements.txt)
  "mysql"       — pymysql (optional; add to requirements.txt to enable)
  "sqlite"      — stdlib sqlite3
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket

_ALLOWED_HOSTS_RAW: str = os.environ.get("TRUST_GATE_SQL_ALLOWED_HOSTS", "")
_ALLOWED_HOSTS: frozenset[str] = frozenset(
    h.strip().lower() for h in _ALLOWED_HOSTS_RAW.split(",") if h.strip()
)
_MAX_ROWS: int = int(os.environ.get("TRUST_GATE_SQL_MAX_ROWS", "20000"))

# Accept SELECT (and CTE WITH … SELECT); reject everything else.
_SELECT_RE = re.compile(r"^\s*(WITH\b.*?SELECT\b|SELECT\b)", re.IGNORECASE | re.DOTALL)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


class SqlConnectorError(ValueError):
    """Raised for configuration or query validation failures."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def query_table(
    *,
    driver: str,
    host: str = "",
    port: int | None = None,
    database: str,
    username: str = "",
    password: str = "",
    query: str,
    table_name: str | None = None,
) -> tuple[str, dict[str, list[dict]]]:
    """Run a SELECT query and return ``("tabular", {table_name: [rows]})``.

    ``table_name`` overrides the key in the returned dict; defaults to the
    literal ``"query_result"``.
    """
    _validate_query(query)
    if driver != "sqlite":
        _validate_host(host)

    limited_query = _inject_limit(query, _MAX_ROWS)

    if driver == "postgresql":
        rows = _psycopg2(host, port or 5432, database, username, password, limited_query)
    elif driver == "mysql":
        rows = _pymysql(host, port or 3306, database, username, password, limited_query)
    elif driver == "sqlite":
        rows = _sqlite3(database, limited_query)
    else:
        raise SqlConnectorError(
            f"Unknown driver '{driver}'. Choose: postgresql, mysql, sqlite."
        )

    key = table_name or "query_result"
    return "tabular", {key: rows}


def list_tables(
    *,
    driver: str,
    host: str = "",
    port: int | None = None,
    database: str,
    username: str = "",
    password: str = "",
) -> list[str]:
    """Return visible table names (up to 200)."""
    if driver != "sqlite":
        _validate_host(host)

    if driver == "postgresql":
        rows = _psycopg2(
            host, port or 5432, database, username, password,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name LIMIT 200",
        )
        return [r["table_name"] for r in rows]

    if driver == "mysql":
        rows = _pymysql(
            host, port or 3306, database, username, password,
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() ORDER BY table_name LIMIT 200",
        )
        return [r["table_name"] for r in rows]

    if driver == "sqlite":
        rows = _sqlite3(
            database,
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name LIMIT 200",
        )
        return [r["name"] for r in rows]

    raise SqlConnectorError(f"Unknown driver '{driver}'.")


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_host(host: str) -> None:
    if not _ALLOWED_HOSTS:
        raise SqlConnectorError(
            "SQL connector is disabled. "
            "Set TRUST_GATE_SQL_ALLOWED_HOSTS to allowed database host(s), "
            "or '*' to permit all (trusted networks only)."
        )
    if "*" in _ALLOWED_HOSTS:
        return
    if host.lower() not in _ALLOWED_HOSTS:
        raise SqlConnectorError(
            f"Host '{host}' is not in TRUST_GATE_SQL_ALLOWED_HOSTS "
            f"({', '.join(sorted(_ALLOWED_HOSTS))})."
        )
    # Even allowlisted hosts must not resolve to loopback/link-local.
    try:
        addrs = socket.getaddrinfo(host, None)
        for addr in addrs:
            ip = ipaddress.ip_address(addr[4][0])
            if any(ip in net for net in _PRIVATE_NETWORKS):
                raise SqlConnectorError(
                    f"Host '{host}' resolves to a private/loopback address "
                    f"({ip}) which is not permitted."
                )
    except SqlConnectorError:
        raise
    except OSError:
        # DNS failure — not an SSRF concern; let the DB driver surface the error.
        pass


def _validate_query(query: str) -> None:
    if not _SELECT_RE.match(query.strip()):
        raise SqlConnectorError(
            "Only SELECT queries (including CTEs) are permitted. "
            "DDL (CREATE/DROP/ALTER) and DML (INSERT/UPDATE/DELETE) are rejected."
        )


def _inject_limit(query: str, limit: int) -> str:
    """Append LIMIT if the query does not already contain one."""
    stripped = query.rstrip().rstrip(";")
    if re.search(r"\bLIMIT\b", stripped, re.IGNORECASE):
        return stripped
    return f"{stripped} LIMIT {limit}"


# ---------------------------------------------------------------------------
# Driver implementations
# ---------------------------------------------------------------------------

def _psycopg2(
    host: str, port: int, database: str, username: str, password: str, query: str
) -> list[dict]:
    try:
        import psycopg2  # type: ignore[import-untyped]
        import psycopg2.extras  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SqlConnectorError("psycopg2 is not installed.") from exc
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=username,
        password=password,
        connect_timeout=15,
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _pymysql(
    host: str, port: int, database: str, username: str, password: str, query: str
) -> list[dict]:
    try:
        import pymysql  # type: ignore[import-untyped]
        import pymysql.cursors  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SqlConnectorError(
            "pymysql is not installed. Add 'pymysql' to trust-gate requirements.txt."
        ) from exc
    conn = pymysql.connect(
        host=host,
        port=port,
        db=database,
        user=username,
        password=password,
        connect_timeout=15,
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            return list(cur.fetchall())
    finally:
        conn.close()


def _sqlite3(path: str, query: str) -> list[dict]:
    import sqlite3

    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(query)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
