"""Tabular (CSV / Excel / Parquet) processing service.

Tabular data is de-identified by column rule (the ``column:<name>`` matcher)
rather than FHIRPath, since spreadsheet columns are arbitrary.  Each row is a
flat synthetic resource and the configured profile's ``column:`` rules are
applied directly to the matched columns via the standard action registry.
"""

import asyncio
import logging

logger = logging.getLogger("medanon")

_SUPPORTED = ("csv", "xlsx", "parquet")


def _validate_format(file_format: str) -> str:
    fmt = (file_format or "csv").lower()
    if fmt not in _SUPPORTED:
        from pipeline.exceptions import NormalizationError

        raise NormalizationError(
            f"Unsupported tabular format {file_format!r}; expected one of {_SUPPORTED}"
        )
    return fmt


def _engine_deidentify_tabular(
    raw: bytes,
    file_format: str,
    config_profile: str,
    delimiter: str,
    inline_rules: list[dict] | None,
) -> bytes:
    """Parse → apply column rules → serialize (sync; runs in a worker thread).

    When *inline_rules* is provided (the UI column-mapper path) it is used
    directly; otherwise the saved *config_profile* supplies the rules.
    """
    from pipeline.sources import TabularAdapter, apply_column_rules

    adapter = TabularAdapter(file_format, delimiter=delimiter)
    rows = adapter.parse(raw)
    if inline_rules is not None:
        apply_column_rules(rows, rules=inline_rules)
    else:
        from pipeline.config.service import get_settings

        apply_column_rules(rows, get_settings(config_profile))
    return adapter.serialize(rows)


def _inspect_tabular(raw: bytes, file_format: str, delimiter: str) -> dict:
    """Parse and return a column preview (no de-identification)."""
    from pipeline.sources import TabularAdapter

    return TabularAdapter(file_format, delimiter=delimiter).inspect(raw)


class TabularService:
    """De-identify CSV / Excel / Parquet by column rule in a worker thread."""

    async def inspect(
        self, raw: bytes, file_format: str = "csv", delimiter: str = ","
    ) -> dict:
        """Return a column preview (names + sample values) for the UI mapper."""
        fmt = _validate_format(file_format)
        return await asyncio.to_thread(_inspect_tabular, raw, fmt, delimiter)

    async def process_single(
        self,
        raw: bytes,
        file_format: str = "csv",
        config_profile: str = "auto",
        delimiter: str = ",",
        inline_rules: list[dict] | None = None,
    ) -> bytes:
        """De-identify a single tabular file.  Returns bytes in the same format.

        *inline_rules* (UI-authored ``column:`` rules) take precedence over
        *config_profile* when provided.

        Raises:
            NormalizationError: malformed input or missing optional dependency
                (openpyxl for xlsx, pyarrow for parquet).
        """
        fmt = _validate_format(file_format)
        return await asyncio.to_thread(
            _engine_deidentify_tabular,
            raw,
            fmt,
            config_profile,
            delimiter,
            inline_rules,
        )
