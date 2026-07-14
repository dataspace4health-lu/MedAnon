"""Symmetric encryption for stored SQL-source connection passwords.

Connection passwords are persisted in ``medanon.sql_connections`` and must never
be stored in plaintext.  They are encrypted with Fernet (AES-128-CBC + HMAC) under
a key supplied via the ``MEDANON_SQL_CRED_KEY`` environment variable.

Generate a key once and set it in the deployment environment::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

The plaintext password is decrypted only at connect time and is never logged.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_ENV_KEY = "MEDANON_SQL_CRED_KEY"


class CredentialKeyError(RuntimeError):
    """Raised when the credential-encryption key is missing or invalid."""


def _fernet() -> Fernet:
    key = os.environ.get(_ENV_KEY, "").strip()
    if not key:
        raise CredentialKeyError(
            f"{_ENV_KEY} is not set. Generate one with "
            f'`python -c "from cryptography.fernet import Fernet; '
            f'print(Fernet.generate_key().decode())"` and set it in the environment '
            f"to save or use SQL source connections."
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialKeyError(
            f"{_ENV_KEY} is not a valid Fernet key (must be 32 url-safe base64 bytes)."
        ) from exc


def encrypt_secret(plaintext: str) -> str:
    """Encrypt *plaintext* (a connection password) into a storable token."""
    return _fernet().encrypt((plaintext or "").encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt_secret`.

    Raises :class:`CredentialKeyError` when the key is wrong/rotated (so a clear
    operational error surfaces instead of a raw cryptography exception).
    """
    try:
        return _fernet().decrypt((token or "").encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialKeyError(
            f"Stored connection password could not be decrypted  the "
            f"{_ENV_KEY} value may have changed since it was saved."
        ) from exc
