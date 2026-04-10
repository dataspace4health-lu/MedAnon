"""CLI everything subcommand — fetch via FHIR $everything."""

import json
import os
from pathlib import Path

from rich import print

from integrations.fhir.client import fetch_everything
import pipeline.config as config
from pipeline.processor import process_data


def add_everything_args(p):
    """Add everything-subcommand arguments to an argparse parser."""
    p.add_argument(
        "--server",
        help="FHIR base URL (overrides FHIR_SOURCE_URL env). "
        "E.g. http://host:8080/fhir",
    )
    p.add_argument(
        "--resource-type",
        required=True,
        dest="resource_type",
        help="Resource type for $everything, e.g. Patient.",
    )
    p.add_argument(
        "--id", required=True, dest="resource_id", help="Resource ID, e.g. DDME."
    )
    p.add_argument("--output", required=True, help="Output NDJSON file path.")
    p.add_argument(
        "--config",
        "-c",
        dest="config_filename",
        help="YAML config file for anonymization rules.",
    )
    p.add_argument(
        "--params",
        dest="extra_params",
        help="Additional query params, e.g. '_count=50'.",
    )
    p.add_argument(
        "--token",
        dest="fhir_token",
        help="Bearer token for FHIR server auth (overrides FHIR_SOURCE_TOKEN env).",
    )
    p.add_argument(
        "--timeout", type=float, default=30.0, help="HTTP timeout in seconds."
    )
    return p


def run_everything(args):
    server = (args.server or os.environ.get("FHIR_SOURCE_URL", "")).rstrip("/")
    if not server:
        raise SystemExit("error: --server or FHIR_SOURCE_URL env var is required")
    token = args.fhir_token or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = args.timeout

    query_params = {}
    if args.extra_params:
        for part in args.extra_params.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query_params[k.strip()] = v.strip()

    settings = None
    if args.config_filename:
        settings = config.Settings(args.config_filename)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with open(output_path, "w", encoding="utf-8") as fout:
        for resource in fetch_everything(
            server,
            args.resource_type,
            args.resource_id,
            params=query_params or None,
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

    print(f":thumbs_up: $everything: {total} resource(s) → {output_path}")
