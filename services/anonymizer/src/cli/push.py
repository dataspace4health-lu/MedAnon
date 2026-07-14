"""CLI push subcommand  upload resources to a FHIR server."""

import os
from pathlib import Path

from rich import print

from integrations.fhir.client import upload_resources
import pipeline.config as config
from pipeline.io_formats import detect_format, read_input_file
from pipeline.processor import process_data


def add_push_args(p):
    """Add push-subcommand arguments to an argparse parser."""
    p.add_argument(
        "input_file",
        nargs="?",
        default=None,
        help="Input file (NDJSON or JSON). Each line / resource is uploaded individually.",
    )
    p.add_argument(
        "--input",
        dest="input_flag",
        default=None,
        help="Input file (alternative to positional argument).",
    )
    p.add_argument(
        "--server",
        help="Target FHIR base URL (overrides FHIR_TARGET_URL env). "
        "E.g. http://host:8080/fhir",
    )
    p.add_argument(
        "--config",
        "-c",
        dest="config_filename",
        help="YAML config for de-identification rules. If omitted, resources are uploaded as-is.",
    )
    p.add_argument(
        "--token",
        dest="fhir_token",
        help="Bearer token for target FHIR server auth (overrides FHIR_TARGET_TOKEN env).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default 30).",
    )
    return p


def run_push(args):
    server = (args.server or os.environ.get("FHIR_TARGET_URL", "")).rstrip("/")
    if not server:
        raise SystemExit("error: --server or FHIR_TARGET_URL env var is required")
    token = args.fhir_token or os.environ.get("FHIR_TARGET_TOKEN")
    timeout = args.timeout

    settings = None
    if args.config_filename:
        settings = config.Settings(args.config_filename)

    input_file = args.input_file or getattr(args, "input_flag", None)
    if not input_file:
        raise SystemExit(2)
    input_path = Path(input_file)
    chosen_format = detect_format(str(input_path), "auto")
    resource_or_list = read_input_file(str(input_path), chosen_format)

    if isinstance(resource_or_list, list):
        resources = resource_or_list
    else:
        resources = [resource_or_list]

    def _deidentify_and_yield():
        for res in resources:
            if settings is not None:
                res = process_data(res, settings)
            yield res

    total = errors = 0
    for result in upload_resources(
        server, _deidentify_and_yield(), token=token, timeout=timeout
    ):
        total += 1
        if result["success"]:
            print(
                f"  [green]OK[/green] {result['resourceType']} "
                f"{result['source_id']} → server id {result['server_id']}"
            )
        else:
            errors += 1
            print(
                f"  [red]ERR[/red] {result['resourceType']} "
                f"{result['source_id']}: {result['error']}"
            )

    status = ":thumbs_up:" if errors == 0 else ":warning:"
    print(f"{status} Pushed {total} resource(s) to {server}  {errors} error(s)")
