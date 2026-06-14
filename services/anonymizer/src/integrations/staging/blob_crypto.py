"""Opt-in encrypted body staging (A1) — gzip + Fernet for staged FHIR resources.

By default the staging table stores **references only** (no PHI at rest); Phase 2
re-fetches bodies from the source FHIR server. When ``MEDANON_STAGE_BODIES=encrypted``
is set, Phase 1 instead persists each resource body **gzipped and Fernet-encrypted**
(AES-128-CBC + HMAC) under ``MEDANON_STAGE_BLOB_KEY``, so Phase 2 reads + decrypts
in-process instead of re-fetching. Plaintext PHI never touches disk; rows expire via
the existing ``expires_at`` TTL.

This mirrors ``integrations/sql_source/secrets.py`` (the established Fernet pattern)
but operates on bytes (gzipped JSON) rather than short strings, and uses a dedicated
key so blob and SQL-credential keys can be rotated independently.

Fail-closed: when bodies are requested but no valid key is configured, encryption
raises ``StageBlobKeyError`` so a misconfiguration can never silently write
plaintext or skip protection.
"""

from __future__ import annotations

import gzip
import json
import os

from cryptography.fernet import Fernet, InvalidToken

from utils.json_fast import dumps_bytes as _json_dumps_bytes

_MODE_ENV = "MEDANON_STAGE_BODIES"
_KEY_ENV = "MEDANON_STAGE_BLOB_KEY"


class StageBlobKeyError(RuntimeError):
    """Raised when body staging is enabled but the blob key is missing/invalid."""


def stage_bodies_enabled() -> bool:
    """True when ``MEDANON_STAGE_BODIES=encrypted`` (the only supported on value)."""
    return os.environ.get(_MODE_ENV, "").strip().lower() == "encrypted"


def _fernet() -> Fernet:
    key = os.environ.get(_KEY_ENV, "").strip()
    if not key:
        raise StageBlobKeyError(
            f"{_MODE_ENV}=encrypted requires {_KEY_ENV}. Generate one with "
            f'`python -c "from cryptography.fernet import Fernet; '
            f'print(Fernet.generate_key().decode())"` and set it in the environment.'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise StageBlobKeyError(
            f"{_KEY_ENV} is not a valid Fernet key (32 url-safe base64 bytes)."
        ) from exc


def encrypt_resource(resource: dict) -> bytes:
    """gzip + encrypt a FHIR resource dict into a storable blob (bytes)."""
    raw = _json_dumps_bytes(resource)
    packed = gzip.compress(raw, compresslevel=6)
    return _fernet().encrypt(packed)


def decrypt_resource(blob: bytes) -> dict:
    """Reverse :func:`encrypt_resource`. Raises ``StageBlobKeyError`` on bad key."""
    try:
        packed = _fernet().decrypt(bytes(blob))
    except InvalidToken as exc:
        raise StageBlobKeyError(
            f"Staged resource blob could not be decrypted — the {_KEY_ENV} value "
            f"may have changed since it was staged."
        ) from exc
    return json.loads(gzip.decompress(packed))
