"""Connector layer: parse files (CSV/Excel/NDJSON/JSON) and query SQL databases.

These connectors normalise external data sources into the same ``list[dict]``
(FHIR) or ``dict[str, list[dict]]`` (tabular/OMOP) shapes accepted by the
core assess engine, so the full check suite runs identically regardless of
input origin.
"""
from connectors.file_connector import ConnectorError, parse_file
from connectors.sql_connector import SqlConnectorError, list_tables, query_table

__all__ = [
    "ConnectorError",
    "SqlConnectorError",
    "parse_file",
    "list_tables",
    "query_table",
]
