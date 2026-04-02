"""Subscriptions sub-package.

The module-level singleton ``_sub_store`` lives here (in ``__init__``) so that
existing code like ``import pipeline.subscriptions as sub_mod; sub_mod._sub_store``
continues to read/write the authoritative reference.

The ``SqliteSubscriptionStore`` class is defined in ``store.py``.
The ``dispatch_subscriptions`` function is in ``dispatcher.py``.
"""

from pipeline.subscriptions.store import SqliteSubscriptionStore  # noqa: F401
from typing import Optional

_sub_store: Optional[SqliteSubscriptionStore] = None


def init_subscription_store(db_path: str | None = None) -> SqliteSubscriptionStore:
    """Initialise the module-level subscription store singleton and return it."""
    global _sub_store
    _sub_store = SqliteSubscriptionStore(db_path)
    return _sub_store


def get_subscription_store() -> Optional[SqliteSubscriptionStore]:
    """Return the current module-level store, or None if not yet initialised."""
    return _sub_store
