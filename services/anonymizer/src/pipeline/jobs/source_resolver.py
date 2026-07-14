"""Runtime resolution of the input-source bearer token.

Export jobs may reference a saved input source by ``source_id``. The source's
``server_url`` is injected into ``job.params`` at submit time (a URL is not a
secret), but the bearer token is kept **encrypted** in the source store and
decrypted only here, in the worker process, at fetch time  it is never persisted
in the job store.

Resolution order:

1. ``source_id`` -> decrypt the stored token (when the source has one).
2. explicit ``params["token"]`` (legacy per-request token).
3. ``FHIR_SOURCE_TOKEN`` env default.
"""

from __future__ import annotations

import os


def _saved_server_token(server_id: str) -> str | None:
    """Decrypt the stored token for a saved FHIR server, or None."""
    from integrations.connectors import get_source_store

    store = get_source_store()
    if store is None:
        return None
    enc = store.get_encrypted_token(server_id)
    if not enc:
        return None
    from integrations.sql_source.secrets import decrypt_secret

    return decrypt_secret(enc)


def resolve_source_token(params: dict | None) -> str | None:
    """Return the FHIR bearer token for this job, decrypting a saved source token."""
    params = params or {}
    source_id = params.get("source_id")
    if source_id:
        tok = _saved_server_token(source_id)
        if tok is not None:
            return tok
    return params.get("token") or os.environ.get("FHIR_SOURCE_TOKEN")


def resolve_target_token(params: dict | None) -> str | None:
    """Return the bearer token for the upload TARGET, decrypting a saved server token.

    Order: saved ``target_id`` token (server-side) -> explicit ``target_token`` ->
    ``FHIR_TARGET_TOKEN`` env. Mirrors :func:`resolve_source_token`.
    """
    params = params or {}
    target_id = params.get("target_id")
    if target_id:
        tok = _saved_server_token(target_id)
        if tok is not None:
            return tok
    return params.get("target_token") or os.environ.get("FHIR_TARGET_TOKEN")
