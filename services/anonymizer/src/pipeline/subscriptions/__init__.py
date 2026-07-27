"""Subscriptions sub-package.

The module-level singleton ``_sub_store`` lives here (in ``__init__``) so that
existing code like ``import pipeline.subscriptions as sub_mod; sub_mod._sub_store``
continues to read/write the authoritative reference.

The ``SqliteSubscriptionStore`` class is defined in ``store.py``.
"""

from pipeline.subscriptions.store import SqliteSubscriptionStore  # noqa: F401

_sub_store = None


def init_subscription_store(db_path: str | None = None, store=None):
    """Initialise the module-level subscription store singleton.

    If *store* is provided (e.g. a PostgresSubscriptionStore), it is used
    directly.  Otherwise a SqliteSubscriptionStore is created at *db_path*.
    """
    global _sub_store
    _sub_store = store if store is not None else SqliteSubscriptionStore(db_path)
    return _sub_store


def get_subscription_store():
    """Return the current module-level store, or None if not yet initialised."""
    return _sub_store
