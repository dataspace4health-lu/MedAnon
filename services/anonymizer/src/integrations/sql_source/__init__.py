"""SQL source integration  read-only ingestion from external SQL databases.

PostgreSQL-only for the MVP (reuses the already-present ``psycopg2-binary``).
The de-identification engine is unchanged: a table row is normalised to a flat
``{"resourceType": "TabularRow", <col>: value, …}`` dict, so the existing
``pipeline.sources.apply_column_rules`` (incl. the gPAS batch pass) processes it.

Submodules:
  * ``secrets``   Fernet encrypt/decrypt for stored connection passwords.
  * ``connect``   host allow-list guard + read-only connection helper.
  * ``reflect``   schema reflection (tables / columns) + chunked row reader.
"""

from __future__ import annotations

from integrations.sql_source.connect import (
    SqlSourceError,
    assert_host_allowed,
    open_readonly_connection,
)
from integrations.sql_source.reflect import (
    describe_table,
    iter_rows,
    list_tables,
    primary_key,
)
from integrations.sql_source.secrets import decrypt_secret, encrypt_secret

__all__ = [
    "SqlSourceError",
    "assert_host_allowed",
    "open_readonly_connection",
    "list_tables",
    "describe_table",
    "primary_key",
    "iter_rows",
    "encrypt_secret",
    "decrypt_secret",
]
