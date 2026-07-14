"""CLI export subcommand  FHIR Bulk Data Export ($export)."""

import json
import os
from pathlib import Path

from rich import print

from integrations.fhir.client import bulk_export
import pipeline.config as config
from pipeline.processor import process_data


def add_export_args(p):
    """Add export-subcommand arguments to an argparse parser."""
    p.add_argument(
        "--server",
        help="FHIR base URL (overrides FHIR_SOURCE_URL env). "
        "E.g. http://host:8080/fhir",
    )
    p.add_argument(
        "--level",
        choices=["system", "type"],
        default="system",
        help="Export level: 'system' for /$export, 'type' for /{Type}/$export.",
    )
    p.add_argument(
        "--resource-type",
        dest="resource_type",
        help="Resource type for type-level export. "
        "For system-level, sets the _type filter parameter.",
    )
    p.add_argument(
        "--type-filter",
        dest="type_filter",
        help="Comma-separated _type filter for system-level export "
        "(e.g. Patient,Observation). Overrides --resource-type for system level.",
    )
    p.add_argument(
        "--since",
        help="Only export resources modified after this instant (sets _since param).",
    )
    p.add_argument("--output", required=True, help="Output NDJSON file path.")
    p.add_argument(
        "--config",
        "-c",
        dest="config_filename",
        help="YAML config file for anonymization rules.",
    )
    p.add_argument(
        "--token",
        dest="fhir_token",
        help="Bearer token for FHIR server auth (overrides FHIR_SOURCE_TOKEN env).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout per request in seconds.",
    )
    return p


def run_export(args):
    server = (args.server or os.environ.get("FHIR_SOURCE_URL", "")).rstrip("/")
    if not server:
        raise SystemExit("error: --server or FHIR_SOURCE_URL env var is required")
    token = args.fhir_token or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = args.timeout

    if args.level == "type" and not args.resource_type:
        raise SystemExit("error: --resource-type is required for type-level export")

    settings = None
    if args.config_filename:
        settings = config.Settings(args.config_filename)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with open(output_path, "w", encoding="utf-8") as fout:
        for resource in bulk_export(
            server,
            level=args.level,
            resource_type=args.resource_type,
            type_filter=args.type_filter,
            since=args.since,
            token=token,
            timeout=timeout,
        ):
            if settings is not None:
                resource = process_data(resource, settings)
            fout.write(json.dumps(resource, separators=(",", ":")))
            fout.write("\n")
            total += 1
            if total % 100 == 0:
                print(f"  processed {total} resources...")

    print(f":thumbs_up: Bulk export: {total} resource(s) → {output_path}")
