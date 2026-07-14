"""CLI entry point  thin dispatcher to command sub-modules."""

import argparse
import json
import sys
from pathlib import Path

from rich import print

from integrations.gpas.client import list_gpas_domains
import pipeline.config as config
from pipeline.io_formats import detect_format, read_input_file, write_output_file
from pipeline.processor import process_data

from cli.fetch import add_fetch_args, run_fetch
from cli.everything import add_everything_args, run_everything
from cli.push import add_push_args, run_push
from cli.export import add_export_args, run_export
from cli.cohort import add_cohort_args, run_cohort


def _load_settings(
    config_filename,
    match,
    action,
    gpas_url,
    gpas_domain,
    gpas_operation,
    gpas_token,
    gpas_timeout_sec,
    gpas_admin_url,
):
    if config_filename:
        return config.Settings(config_filename)

    if not gpas_url or not gpas_domain:
        raise ValueError("Provide either --config or both --gpas-url and --gpas-domain")

    params = {
        "gpas_url": gpas_url,
        "gpas_domain": gpas_domain,
        "gpas_operation": gpas_operation,
        "gpas_timeout_sec": gpas_timeout_sec,
    }
    if gpas_admin_url:
        params["gpas_admin_url"] = gpas_admin_url
    if gpas_token:
        params["gpas_token"] = gpas_token

    return type(
        "CliSettings",
        (),
        {
            "rules": [
                {
                    "match": match,
                    "action": action,
                    "params": params,
                }
            ]
        },
    )()


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Black-box FHIR processor for JSON, NDJSON, XML, and Bundles.",
    )
    parser.add_argument(
        "input_file", nargs="?", help="Input FHIR file (JSON, NDJSON, or XML)."
    )
    parser.add_argument(
        "output_file", nargs="?", help="Output file (JSON, NDJSON, or XML)."
    )
    parser.add_argument(
        "--config",
        "-c",
        dest="config_filename",
        help="YAML config file. If omitted, gPAS options must be provided.",
    )
    parser.add_argument(
        "--format",
        default="auto",
        choices=["auto", "json", "ndjson", "xml"],
        help="Set both input and output format.",
    )
    parser.add_argument(
        "--input-format",
        default="auto",
        choices=["auto", "json", "ndjson", "xml"],
        help="Input format override.",
    )
    parser.add_argument(
        "--output-format",
        default="auto",
        choices=["auto", "json", "ndjson", "xml"],
        help="Output format override.",
    )
    parser.add_argument(
        "--strip-line-prefix",
        default="//",
        help="Prefix to strip from NDJSON lines before parsing.",
    )
    parser.add_argument(
        "--match",
        default="Patient.id",
        help="FHIRPath match used when building an in-memory gPAS rule.",
    )
    parser.add_argument(
        "--action",
        default="gpas_pseudonymize",
        help="Action used when building an in-memory rule.",
    )
    parser.add_argument("--gpas-url", help="gPAS base URL.")
    parser.add_argument(
        "--gpas-admin-url", help="Optional gPAS admin UI URL used for domain discovery."
    )
    parser.add_argument("--gpas-domain", help="gPAS domain name.")
    parser.add_argument(
        "--gpas-operation",
        default="pseudonymizeAllowCreate",
        help="gPAS operation: pseudonymizeAllowCreate, pseudonymize, or dePseudonymize.",
    )
    parser.add_argument("--gpas-token", help="Optional gPAS bearer token.")
    parser.add_argument(
        "--gpas-timeout-sec", type=float, default=30.0, help="gPAS timeout in seconds."
    )
    parser.add_argument(
        "--gpas-list-domains",
        action="store_true",
        help="List available gPAS domains and exit.",
    )
    return parser


_SUBCOMMANDS = {
    "fetch": ("cli.py fetch", None, add_fetch_args, run_fetch),
    "everything": (
        "cli.py everything",
        "Fetch all resources via FHIR $everything, optionally de-identify, and save as NDJSON.",
        add_everything_args,
        run_everything,
    ),
    "push": (
        "cli.py push",
        "De-identify a local FHIR file and upload resources to a FHIR server.",
        add_push_args,
        run_push,
    ),
    "export": (
        "cli.py export",
        "Bulk export FHIR resources via $export, optionally de-identify, and save as NDJSON.",
        add_export_args,
        run_export,
    ),
    "cohort": (
        "cli.py cohort",
        "Export all records for patients matching a condition code via $everything.",
        add_cohort_args,
        run_cohort,
    ),
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "process":
        argv = argv[1:]

    # Route to subcommand modules
    if argv and argv[0] in _SUBCOMMANDS:
        prog, desc, add_args_fn, run_fn = _SUBCOMMANDS[argv[0]]
        sub_parser = argparse.ArgumentParser(prog=prog, description=desc)
        add_args_fn(sub_parser)
        sub_args = sub_parser.parse_args(argv[1:])
        run_fn(sub_args)
        return

    # Default: process command
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.gpas_list_domains:
        if not args.gpas_url and not args.gpas_admin_url:
            parser.error("--gpas-list-domains requires --gpas-url or --gpas-admin-url")
        domain_params = {
            "gpas_timeout_sec": args.gpas_timeout_sec,
        }
        if args.gpas_url:
            domain_params["gpas_url"] = args.gpas_url
        if args.gpas_admin_url:
            domain_params["gpas_admin_url"] = args.gpas_admin_url
        if args.gpas_token:
            domain_params["gpas_token"] = args.gpas_token

        for domain in list_gpas_domains(domain_params):
            print(domain)
        return

    if not args.input_file or not args.output_file:
        parser.error(
            "input_file and output_file are required unless --gpas-list-domains is used"
        )

    settings = _load_settings(
        config_filename=args.config_filename,
        match=args.match,
        action=args.action,
        gpas_url=args.gpas_url,
        gpas_domain=args.gpas_domain,
        gpas_operation=args.gpas_operation,
        gpas_token=args.gpas_token,
        gpas_timeout_sec=args.gpas_timeout_sec,
        gpas_admin_url=args.gpas_admin_url,
    )
    input_format_req = args.input_format if args.input_format != "auto" else args.format
    output_format_req = (
        args.output_format if args.output_format != "auto" else args.format
    )
    chosen_input_format = detect_format(args.input_file, input_format_req)
    chosen_output_format = detect_format(args.output_file, output_format_req)

    input_path = Path(args.input_file)
    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Stream NDJSON line-by-line to avoid loading entire file into memory
    if chosen_input_format == "ndjson":
        total = 0
        with (
            open(input_path, "r", encoding="utf-8-sig") as fin,
            open(output_path, "w", encoding="utf-8") as fout,
        ):
            for raw_line in fin:
                line = raw_line.strip()
                if not line:
                    continue
                if args.strip_line_prefix and line.startswith(args.strip_line_prefix):
                    line = line[len(args.strip_line_prefix) :]
                resource = json.loads(line)
                result = process_data(resource, settings)
                fout.write(json.dumps(result, separators=(",", ":")))
                fout.write("\n")
                total += 1
                if total % 100 == 0:
                    print(f"  processed {total} resources...")
    else:
        resource = read_input_file(
            str(input_path), chosen_input_format, args.strip_line_prefix
        )
        ret = process_data(resource, settings)
        write_output_file(ret, str(output_path), chosen_output_format)

    print(f":thumbs_up: Wrote output file {output_path}")


if __name__ == "__main__":
    main()
