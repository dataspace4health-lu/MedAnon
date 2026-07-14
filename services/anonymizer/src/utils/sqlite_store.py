"""WAL-mode SQLite connections that are actually closed.

``sqlite3.Connection.__exit__`` commits or rolls back the *transaction*.  It
does **not** close the connection -- a long-standing Python gotcha.  Six stores
in this codebase wrote::

    with self._connect() as conn:      # commits, never closes
        conn.execute(...)

and leaked one file descriptor per call until the garbage collector ran.  Under
``SqliteJobStore`` the worker polls ``next_pending()`` every two seconds, so the
leak is continuous.  The test suite has been reporting it all along, as 102
``ResourceWarning: unclosed database`` lines nobody read.

:func:`connect` is a context manager that does both: the transaction is
committed (or rolled back on an exception), and the connection is closed on
every exit path.  Call sites keep their ``with self._connect() as conn:`` shape.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

# Long enough to ride out a WAL checkpoint under concurrent readers, short
# enough that a wedged writer surfaces as an error rather than a hang.
_DEFAULT_TIMEOUT_SEC = 10


@contextmanager
def connect(
    path: str, *, timeout: float = _DEFAULT_TIMEOUT_SEC
) -> Iterator[sqlite3.Connection]:
    """Yield a WAL-mode connection to *path*; commit, then close, always.

    ``row_factory`` is :class:`sqlite3.Row`, whose rows hold plain values rather
    than a cursor reference, so results stay valid after the connection closes.

    The ``PRAGMA journal_mode=WAL`` runs before the transaction block: a pragma
    cannot change the journal mode from inside an open transaction.
    """
    conn = sqlite3.connect(path, check_same_thread=False, timeout=timeout)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        with conn:  # commit on success, rollback on exception
            yield conn
    finally:
        conn.close()
