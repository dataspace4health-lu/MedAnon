"""Connector services  saved input sources + S3 output destinations.

Secrets (FHIR bearer tokens, S3 secret keys) are encrypted before they reach the
store and decrypted only transiently to test or use a connection. Both stores are
PostgreSQL-only; a clear error surfaces when the app DB is not configured.

Mirrors :mod:`api.services.sql_source`.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("medanon")


class ConnectorNotFound(Exception):
    """Raised when a referenced source/destination id does not exist."""


class ConnectorStoreUnavailable(Exception):
    """Raised when the connector store is not initialised (no app DB)."""


def _source_store():
    from pipeline.connectors import get_source_store

    store = get_source_store()
    if store is None:
        raise ConnectorStoreUnavailable(
            "Input-source store is not initialised  set MEDANON_APP_DB_URL "
            "(PostgreSQL) to enable saved input sources."
        )
    return store


def _destination_store():
    from pipeline.connectors import get_destination_store

    store = get_destination_store()
    if store is None:
        raise ConnectorStoreUnavailable(
            "Output-destination store is not initialised  set MEDANON_APP_DB_URL "
            "(PostgreSQL) to enable saved S3 destinations."
        )
    return store


class SourceService:
    """Manage saved input sources (FHIR servers) and test their reachability."""

    def create(
        self,
        *,
        name: str,
        server_url: str,
        token: str = "",
        kind: str = "fhir",
        role: str = "source",
    ) -> dict:
        from integrations.sql_source.secrets import encrypt_secret

        token_enc = encrypt_secret(token) if token else None
        return _source_store().create(
            name=name,
            server_url=server_url,
            token_enc=token_enc,
            kind=kind,
            role=role,
        )

    def list(self, role: str | None = None) -> list[dict]:
        return _source_store().list_all(role=role)

    def delete(self, source_id: str) -> bool:
        return _source_store().delete(source_id)

    async def test(self, source_id: str) -> dict:
        """Probe the FHIR server's CapabilityStatement. Returns {ok, resource_types}."""
        return await asyncio.to_thread(self._test_sync, source_id)

    def _test_sync(self, source_id: str) -> dict:
        from integrations.fhir.reader import get_capability_statement
        from integrations.sql_source.secrets import decrypt_secret

        store = _source_store()
        meta = store.get(source_id)
        if meta is None:
            raise ConnectorNotFound(source_id)
        enc = store.get_encrypted_token(source_id)
        token = decrypt_secret(enc) if enc else None
        types = get_capability_statement(meta["server_url"], token=token, timeout=10)
        return {"ok": True, "resource_types": len(types)}


class DestinationService:
    """Manage saved S3 output destinations and test bucket access."""

    def create(
        self,
        *,
        name: str,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str | None = None,
        key_prefix: str = "",
        secure: bool = True,
        path_style: bool = False,
    ) -> dict:
        from integrations.sql_source.secrets import encrypt_secret

        return _destination_store().create(
            name=name,
            endpoint=endpoint,
            bucket=bucket,
            access_key=access_key,
            secret_key_enc=encrypt_secret(secret_key),
            region=region,
            key_prefix=key_prefix,
            secure=secure,
            path_style=path_style,
        )

    def list(self) -> list[dict]:
        return _destination_store().list_all()

    def delete(self, dest_id: str) -> bool:
        return _destination_store().delete(dest_id)

    async def test(self, dest_id: str) -> dict:
        """Connect and ensure the bucket exists. Returns {ok, bucket, created}."""
        return await asyncio.to_thread(self._test_sync, dest_id)

    def _test_sync(self, dest_id: str) -> dict:
        from integrations.sql_source.secrets import decrypt_secret
        from integrations.storage.s3 import make_minio_client

        store = _destination_store()
        meta = store.get(dest_id)
        if meta is None:
            raise ConnectorNotFound(dest_id)
        enc = store.get_encrypted_secret(dest_id)
        client = make_minio_client(
            meta["endpoint"],
            meta["access_key"],
            decrypt_secret(enc) if enc else "",
            secure=bool(meta.get("secure", True)),
            region=meta.get("region"),
        )
        created = False
        if not client.bucket_exists(meta["bucket"]):
            client.make_bucket(meta["bucket"])
            created = True
        return {"ok": True, "bucket": meta["bucket"], "created": created}
