"""CLI cohort subcommand — condition-based cohort export via $everything."""

import json
import os
from pathlib import Path

from rich import print

from integrations.fhir.client import fetch_cohort
import pipeline.config as config
from pipeline.processor import process_data


def add_cohort_args(p):
    """Add cohort-subcommand arguments to an argparse parser."""
    p.add_argument("--server", help="FHIR base URL (overrides FHIR_SOURCE_URL env).")
    p.add_argument(
        "--search-type",
        dest="search_type",
        default="Condition",
        help="Resource type to search for cohort selection (default: Condition).",
    )
    p.add_argument(
        "--code",
        required=True,
        help="Code to search for, e.g. 'E11' or 'http://hl7.org/fhir/sid/icd-10|E11'.",
    )
    p.add_argument(
        "--search-params",
        dest="search_extra_params",
        help="Additional search params, e.g. 'clinical-status=active&verification-status=confirmed'.",
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


def run_cohort(args):
    server = (args.server or os.environ.get("FHIR_SOURCE_URL", "")).rstrip("/")
    if not server:
        raise SystemExit("error: --server or FHIR_SOURCE_URL env var is required")
    token = args.fhir_token or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = args.timeout

    # Build search params
    search_params = {"code": args.code}
    if args.search_extra_params:
        for part in args.search_extra_params.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                search_params[k.strip()] = v.strip()

    settings = None
    if args.config_filename:
        settings = config.Settings(args.config_filename)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    with open(output_path, "w", encoding="utf-8") as fout:
        for resource in fetch_cohort(
            server,
            args.search_type,
            search_params,
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

    print(f":thumbs_up: Cohort export: {total} resource(s) → {output_path}")
