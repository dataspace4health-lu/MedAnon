"""Read-only connection helper + host allow-list guard for SQL sources.

Unlike the FHIR SSRF guard (``api/deps.check_hostname_ssrf``), which *blocks*
private/loopback addresses, a SQL source is normally an internal database server,
so blocking private IPs would defeat the feature.  Instead we enforce an explicit
**host allow-list** via ``MEDANON_SQL_SOURCE_ALLOWED_HOSTS``.

The list is comma-separated.  An entry matches when it equals the host exactly,
or — when it begins with a leading dot — when the host ends with it::

    MEDANON_SQL_SOURCE_ALLOWED_HOSTS=localhost,app-db,.internal.example.com

An **empty/unset** list denies every connection (secure default): the operator
must opt hosts in explicitly.

Connections are opened **read-only** (``default_transaction_read_only=on``) with a
bounded ``statement_timeout`` and ``connect_timeout`` so a misbehaving source
cannot stall a worker.
"""

from __future__ import annotations

import logging
import os

import psycopg2
import psycopg2.extras

_log = logging.getLogger("medanon.sql_source")

_ALLOWED_HOSTS_ENV = "MEDANON_SQL_SOURCE_ALLOWED_HOSTS"
_CONNECT_TIMEOUT_SEC = int(
    os.environ.get("MEDANON_SQL_SOURCE_CONNECT_TIMEOUT_SEC", "10")
)
_STATEMENT_TIMEOUT_MS = int(
    os.environ.get("MEDANON_SQL_SOURCE_STATEMENT_TIMEOUT_MS", "60000")
)


class SqlSourceError(RuntimeError):
    """Raised for connection / host-policy failures (mapped to HTTP 4xx)."""


def _allowed_hosts() -> list[str]:
    raw = os.environ.get(_ALLOWED_HOSTS_ENV, "")
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def assert_host_allowed(host: str) -> None:
    """Raise :class:`SqlSourceError` unless *host* is permitted by the allow-list."""
    host = (host or "").strip().lower()
    if not host:
        raise SqlSourceError("Database host is required.")
    allowed = _allowed_hosts()
    if not allowed:
        raise SqlSourceError(
            f"No SQL source hosts are permitted. Set {_ALLOWED_HOSTS_ENV} "
            f"(comma-separated) to allow connections."
        )
    for entry in allowed:
        if entry.startswith("."):
            if host == entry[1:] or host.endswith(entry):
                return
        elif host == entry:
            return
    raise SqlSourceError(
        f"Host {host!r} is not in the SQL source allow-list ({_ALLOWED_HOSTS_ENV})."
    )


def open_readonly_connection(conn_info: dict):
    """Open a short-lived **read-only** psycopg2 connection to a source DB.

    *conn_info* keys: ``host``, ``port``, ``dbname``, ``username``, ``password``,
    ``sslmode`` (optional, default ``prefer``).  The host is allow-list checked
    before any network call.  The caller owns the returned connection and must
    close it (use a ``with closing(...)`` or try/finally).
    """
    host = conn_info.get("host", "")
    assert_host_allowed(host)
    try:
        conn = psycopg2.connect(
            host=host,
            port=int(conn_info.get("port") or 5432),
            dbname=conn_info.get("dbname") or "",
            user=conn_info.get("username") or "",
            password=conn_info.get("password") or "",
            sslmode=conn_info.get("sslmode") or "prefer",
            connect_timeout=_CONNECT_TIMEOUT_SEC,
            # Enforce read-only + bounded query time at the session level.
            options=(
                f"-c default_transaction_read_only=on "
                f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}"
            ),
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    except psycopg2.Error as exc:
        # Surface a clean message; never echo the password.
        raise SqlSourceError(f"Could not connect to source database: {exc}") from exc
    conn.set_session(readonly=True, autocommit=True)
    return conn
