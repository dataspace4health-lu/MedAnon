"""CLI fetch subcommand — download FHIR resources from a server."""
import json
import os
from pathlib import Path

from rich import print

from integrations.fhir.client import (
    fetch_all_resource_types,
    get_capability_statement,
)
import pipeline.config as config
from pipeline.processor import process_data


def _resource_types_from_config(settings) -> set:
    """Return FHIR resource type names inferred from the leading segment of rule match expressions.

    E.g. a rule matching ``Patient.name`` contributes ``Patient``.
    """
    types: set = set()
    for rule in getattr(settings, "rules", []) or []:
        match_expr = rule.get("match") if isinstance(rule, dict) else getattr(rule, "match", None)
        if match_expr:
            first = str(match_expr).split(".")[0]
            if first:
                types.add(first)
    return types


def add_fetch_args(p):
    """Add fetch-subcommand arguments to an argparse parser."""
    p.add_argument("--server",
                   help="FHIR base URL (overrides FHIR_SOURCE_URL env). "
                        "E.g. http://host:8080/fhir")
    p.add_argument("--resource-type", dest="resource_types",
                   help="Comma-separated resource types to fetch. Omit to discover from /metadata.")
    p.add_argument("--output", required=True,
                   help="Output NDJSON file path.")
    p.add_argument("--config", "-c", dest="config_filename",
                   help="YAML config file for anonymization rules.")
    p.add_argument("--count", type=int, default=None,
                   help="Page size (_count) for FHIR search requests. "
                        "Omit to let the server decide (controlled by FHIR_PAGE_SIZE env var).")
    p.add_argument("--since",
                   help="Only fetch resources modified after this date (sets _lastUpdated param).")
    p.add_argument("--params", dest="extra_params",
                   help="Additional FHIR query params, e.g. '_tag=study-cohort'.")
    p.add_argument("--token", dest="fhir_token",
                   help="Bearer token for FHIR server auth (overrides FHIR_SOURCE_TOKEN env).")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="HTTP timeout in seconds.")
    p.add_argument("--discover", "--discover-only", action="store_true",
                   help="List available resource types from /metadata, write to --output, and exit.")
    return p


def run_fetch(args):
    server = (args.server or os.environ.get("FHIR_SOURCE_URL", "")).rstrip("/")
    if not server:
        raise SystemExit("error: --server or FHIR_SOURCE_URL env var is required")
    token = args.fhir_token or os.environ.get("FHIR_SOURCE_TOKEN")
    timeout = args.timeout

    # Load settings early so resource type filtering can use config rules
    settings = None
    if args.config_filename:
        settings = config.Settings(args.config_filename)

    if args.discover:
        try:
            resource_types = get_capability_statement(server, token=token, timeout=timeout)
        except ValueError as exc:
            raise SystemExit(f"error: FHIR server error during /metadata: {exc}") from exc
        for rt in resource_types:
            print(rt)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(resource_types) + "\n")
        return

    # Resolve resource types
    if args.resource_types:
        resource_types = [rt.strip() for rt in args.resource_types.split(",") if rt.strip()]
    else:
        try:
            resource_types = get_capability_statement(server, token=token, timeout=timeout)
        except ValueError as exc:
            raise SystemExit(f"error: FHIR server error during /metadata: {exc}") from exc
        # Filter by resource types referenced in config rules when config is provided
        if settings is not None:
            config_types = _resource_types_from_config(settings)
            if config_types:
                resource_types = [rt for rt in resource_types if rt in config_types]
        print("No --resource-type specified, discovering from /metadata...")
        print(f"Found {len(resource_types)} resource type(s): {', '.join(resource_types)}")

    # Build query params
    query_params = {}
    if args.count:
        query_params["_count"] = args.count
    if args.since:
        query_params["_lastUpdated"] = f"ge{args.since}"
    if args.extra_params:
        for part in args.extra_params.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                query_params[k.strip()] = v.strip()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    try:
        with open(output_path, "w", encoding="utf-8") as fout:
            for rt, resource in fetch_all_resource_types(
                server, resource_types, params=query_params, token=token, timeout=timeout
            ):
                if settings is not None:
                    resource = process_data(resource, settings)
                fout.write(json.dumps(resource, separators=(',', ':')))
                fout.write("\n")
                total += 1
                if total % 100 == 0:
                    print(f"  processed {total} resources...")
    except ValueError as exc:
        raise SystemExit(f"error: FHIR server error: {exc}") from exc

    print(f":thumbs_up: Fetched and processed {total} resource(s) → {output_path}")
