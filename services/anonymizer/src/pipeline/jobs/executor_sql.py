"""SQL-export executor — de-identify selected tables of a source database.

Two-job-type sibling of :mod:`pipeline.jobs.executor_tabular`.  The user picks a
saved connection plus a set of tables and a column mapping (a saved profile's
``table:<t>/column:<c>`` rules, or inline rules).  This executor reads each table
in chunks from the **read-only** source connection, de-identifies every row
through the existing column-rule engine, and writes one file per table into a
single result ZIP.

gPAS pseudonymization is consistent across tables (the gPAS TTP persists the
value→pseudonym mapping), so a foreign key pseudonymized in two tables stays
joinable — provided both columns are mapped to the same gPAS domain.

Job params (set by the submit endpoint):
    connection_id   — saved connection to read from
    schema          — source schema (default "public")
    tables          — list of table names to export
    output_format   — "csv" (default) | "ndjson" | "parquet"
    config_profile  — profile whose table:/column: rules to apply (when no inline)
    rules           — inline rule list (takes precedence over config_profile)
    chunk_size      — rows per read chunk (default 1000)

Per-table isolation: a table that fails to read/process is recorded as a
``<table>.error.txt`` entry in the ZIP rather than aborting the whole export.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import zipfile

from domain.jobs import JobStatus
from integrations.storage import publish_result
from pipeline.jobs.checkpoint import save_checkpoint

_log = logging.getLogger("medanon.worker")

_ROW_TYPE = "TabularRow"
_SUPPORTED_FORMATS = ("csv", "ndjson", "parquet")


def _output_dir() -> str:
    return os.environ.get("MEDANON_OUTPUT_DIR", "/output")


def _resolve_connection(connection_id: str) -> dict:
    """Resolve a saved connection into psycopg2 connect kwargs (decrypts password)."""
    from pipeline.sql_connection import get_sql_connection_store
    from integrations.sql_source.secrets import decrypt_secret

    store = get_sql_connection_store()
    if store is None:
        raise ValueError(
            "SQL connection store is not initialised (requires MEDANON_APP_DB_URL)."
        )
    meta = store.get(connection_id)
    if meta is None:
        raise ValueError(f"SQL connection not found: {connection_id!r}")
    enc = store.get_encrypted_password(connection_id)
    return {
        "host": meta["host"],
        "port": meta["port"],
        "dbname": meta["dbname"],
        "username": meta["username"],
        "password": decrypt_secret(enc) if enc else "",
        "sslmode": meta.get("sslmode", "prefer"),
    }


def _load_rules(params: dict) -> list[dict]:
    """Inline rules take precedence; otherwise the saved profile's rules."""
    inline = params.get("rules")
    if inline is not None:
        return list(inline)
    from pipeline.config.service import get_settings

    settings = get_settings(params.get("config_profile") or "auto")
    return list(getattr(settings, "rules", []) or [])


def execute_sql_export(job, store, staging=None) -> None:
    """De-identify selected source tables into one result ZIP (sync; worker thread)."""
    from contextlib import closing

    from integrations.sql_source import (
        iter_rows,
        open_readonly_connection,
        primary_key,
    )
    from pipeline.sources import (
        apply_column_rules,
        resolve_column_manifest,
        rules_for_table,
    )

    params = job.params or {}
    connection_id = params.get("connection_id")
    schema = params.get("schema") or "public"
    tables = params.get("tables") or []
    output_format = (params.get("output_format") or "csv").lower()
    chunk_size = int(params.get("chunk_size") or 1000)

    if not connection_id:
        raise ValueError("sql-export job missing connection_id")
    if not tables:
        raise ValueError("sql-export job has no tables selected")
    if output_format not in _SUPPORTED_FORMATS:
        raise ValueError(
            f"Unsupported output_format {output_format!r}; expected {_SUPPORTED_FORMATS}"
        )

    conn_info = _resolve_connection(connection_id)
    all_rules = _load_rules(params)

    save_checkpoint(
        store, job, {"phase": "processing", "table_count": len(tables), "processed": 0}
    )

    output_path = os.path.join(_output_dir(), f"{job.id}.zip")
    # Transformation manifest as a SEPARATE artifact (one line per table).
    manifest_path = f"{output_path}.manifest.ndjson"
    succeeded = 0
    failed = 0
    table_results: list[dict] = []

    with (
        zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf,
        open(manifest_path, "w", encoding="utf-8") as mfh,
    ):
        for idx, table in enumerate(tables):
            fresh = store.get(job.id)
            if fresh and fresh.status == JobStatus.CANCELLED:
                _log.info("sql_export_cancelled job=%s at=%d", job.id, idx)
                return

            table_rules = rules_for_table(all_rules, table)
            try:
                rows_out, n_rows = _export_one_table(
                    conn_info,
                    schema,
                    table,
                    table_rules,
                    output_format,
                    chunk_size,
                    open_readonly_connection,
                    primary_key,
                    iter_rows,
                    apply_column_rules,
                    closing,
                )
                zf.writestr(_member_name(table, output_format), rows_out)
                mfh.write(
                    json.dumps(
                        {
                            "table": table,
                            "format": "sql",
                            "transformations": resolve_column_manifest(
                                rules=table_rules
                            ),
                        }
                    )
                    + "\n"
                )
                succeeded += 1
                table_results.append({"table": table, "status": "ok", "rows": n_rows})
            except Exception as exc:
                _log.warning(
                    "sql_export_table_failed job=%s table=%s: %s", job.id, table, exc
                )
                zf.writestr(f"{_safe(table)}.error.txt", f"Export failed: {exc}")
                failed += 1
                table_results.append(
                    {"table": table, "status": "error", "error": str(exc)}
                )

            save_checkpoint(
                store,
                job,
                {
                    "phase": "processing",
                    "table_count": len(tables),
                    "processed": idx + 1,
                },
            )

    summary_dict = {
        "total_tables": len(tables),
        "succeeded": succeeded,
        "failed": failed,
        "tables": table_results,
    }
    job.result_path = publish_result(
        job, output_path, manifest_path=manifest_path, audit=summary_dict
    )
    save_checkpoint(
        store,
        job,
        {
            "phase": "done",
            "table_count": len(tables),
            "processed": succeeded + failed,
            "summary": summary_dict,
        },
    )
    _log.info(
        "sql_export_done job=%s tables=%d ok=%d failed=%d",
        job.id,
        len(tables),
        succeeded,
        failed,
    )


def _export_one_table(
    conn_info,
    schema,
    table,
    table_rules,
    output_format,
    chunk_size,
    open_readonly_connection,
    primary_key,
    iter_rows,
    apply_column_rules,
    closing,
) -> tuple[bytes, int]:
    """Read one table in chunks, de-identify, serialize. Returns (bytes, row_count)."""
    with closing(open_readonly_connection(conn_info)) as conn:
        pk = primary_key(conn, schema, table)
        writer = _make_writer(output_format)
        total = 0
        for chunk in iter_rows(conn, schema, table, chunk_size=chunk_size, pk=pk):
            tagged = [{"resourceType": _ROW_TYPE, **row} for row in chunk]
            apply_column_rules(tagged, rules=table_rules)
            cleaned = [
                {k: v for k, v in row.items() if k != "resourceType"} for row in tagged
            ]
            writer.write_rows(cleaned)
            total += len(cleaned)
    return writer.finish(), total


# ---------------------------------------------------------------------------
# Per-format incremental writers
# ---------------------------------------------------------------------------


def _make_writer(output_format: str):
    if output_format == "ndjson":
        return _NdjsonWriter()
    if output_format == "parquet":
        return _ParquetWriter()
    return _CsvWriter()


class _CsvWriter:
    """Streaming CSV writer — header inferred from the first row's columns."""

    def __init__(self) -> None:
        self._buf = io.StringIO()
        self._writer: csv.DictWriter | None = None
        self._columns: list[str] = []

    def write_rows(self, rows: list[dict]) -> None:
        for row in rows:
            if self._writer is None:
                self._columns = list(row.keys())
                self._writer = csv.DictWriter(self._buf, fieldnames=self._columns)
                self._writer.writeheader()
            # Align to the established column set (ignore stragglers, fill gaps).
            self._writer.writerow({c: row.get(c, "") for c in self._columns})

    def finish(self) -> bytes:
        return self._buf.getvalue().encode("utf-8")


class _NdjsonWriter:
    """One JSON object per line."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def write_rows(self, rows: list[dict]) -> None:
        for row in rows:
            self._parts.append(json.dumps(row, default=str, ensure_ascii=False))

    def finish(self) -> bytes:
        return ("\n".join(self._parts) + ("\n" if self._parts else "")).encode("utf-8")


class _ParquetWriter:
    """Buffers rows then writes a single Parquet file (requires pyarrow).

    Note: buffers the whole table in memory — adequate for the MVP; very large
    tables should use CSV/NDJSON streaming.
    """

    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._columns: list[str] = []

    def write_rows(self, rows: list[dict]) -> None:
        for row in rows:
            if not self._columns:
                self._columns = list(row.keys())
            self._rows.append(row)

    def finish(self) -> bytes:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise ValueError(
                "the 'pyarrow' package is required for Parquet export"
            ) from exc
        cols = {c: [str(r.get(c, "")) for r in self._rows] for c in self._columns}
        table = pa.table(cols) if cols else pa.table({})
        buf = io.BytesIO()
        pq.write_table(table, buf)
        return buf.getvalue()


def _safe(name: str) -> str:
    base = os.path.basename(str(name)) or "table"
    return base.replace("/", "_").replace("\\", "_")


def _member_name(table: str, output_format: str) -> str:
    ext = {"csv": "csv", "ndjson": "ndjson", "parquet": "parquet"}[output_format]
    return f"{_safe(table)}.{ext}"
