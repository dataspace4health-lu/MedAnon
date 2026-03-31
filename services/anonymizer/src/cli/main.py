import argparse
import json
import os
import sys
from pathlib import Path

from rich import print

from integrations.gpas.client import list_gpas_domains
from integrations.fhir.client import (
    bulk_export,
    fetch_all_resource_types,
    fetch_cohort,
    fetch_everything,
    get_capability_statement,
    upload_resources,
)
import pipeline.config as config
from pipeline.io_formats import detect_format, read_input_file, write_output_file
from pipeline.processor import process_data


def _load_settings(config_filename, match, action, gpas_url, gpas_domain,
                   gpas_operation, gpas_token, gpas_timeout_sec, gpas_admin_url):
    if config_filename:
        return config.Settings(config_filename)

    if not gpas_url or not gpas_domain:
        raise ValueError(
            "Provide either --config or both --gpas-url and --gpas-domain"
        )

    params = {
        'gpas_url': gpas_url,
        'gpas_domain': gpas_domain,
        'gpas_operation': gpas_operation,
        'gpas_timeout_sec': gpas_timeout_sec,
    }
    if gpas_admin_url:
        params['gpas_admin_url'] = gpas_admin_url
    if gpas_token:
        params['gpas_token'] = gpas_token

    return type('CliSettings', (), {
        'rules': [{
            'match': match,
            'action': action,
            'params': params,
        }]
    })()


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


def _add_fetch_args(p):
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


def _run_fetch(args):
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


def _add_everything_args(p):
    """Add everything-subcommand arguments to an argparse parser."""
    p.add_argument("--server",
                   help="FHIR base URL (overrides FHIR_SOURCE_URL env). "
                        "E.g. http://host:8080/fhir")
    p.add_argument("--resource-type", required=True, dest="resource_type",
                   help="Resource type for $everything, e.g. Patient.")
    p.add_argument("--id", required=True, dest="resource_id",
                   help="Resource ID, e.g. DDME.")
    p.add_argument("--output", required=True,
                   help="Output NDJSON file path.")
    p.add_argument("--config", "-c", dest="config_filename",
                   help="YAML config file for anonymization rules.")
    p.add_argument("--params", dest="extra_params",
                   help="Additional query params, e.g. '_count=50'.")
    p.add_argument("--token", dest="fhir_token",
                   help="Bearer token for FHIR server auth (overrides FHIR_SOURCE_TOKEN env).")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="HTTP timeout in seconds.")
    return p


def _run_everything(args):
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
            server, args.resource_type, args.resource_id,
            params=query_params or None, token=token, timeout=timeout,
        ):
            if settings is not None:
                resource = process_data(resource, settings)
            fout.write(json.dumps(resource, separators=(',', ':')))
            fout.write("\n")
            total += 1
            if total % 100 == 0:
                print(f"  processed {total} resources...")

    print(f":thumbs_up: $everything: {total} resource(s) → {output_path}")


def _add_push_args(p):
    """Add push-subcommand arguments to an argparse parser."""
    p.add_argument("input_file", nargs="?", default=None,
                   help="Input file (NDJSON or JSON). Each line / resource is uploaded individually.")
    p.add_argument("--input", dest="input_flag", default=None,
                   help="Input file (alternative to positional argument).")
    p.add_argument("--server",
                   help="Target FHIR base URL (overrides FHIR_TARGET_URL env). "
                        "E.g. http://host:8080/fhir")
    p.add_argument("--config", "-c", dest="config_filename",
                   help="YAML config for de-identification rules. If omitted, resources are uploaded as-is.")
    p.add_argument("--token", dest="fhir_token",
                   help="Bearer token for target FHIR server auth (overrides FHIR_TARGET_TOKEN env).")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="HTTP timeout in seconds (default 30).")
    return p


def _run_push(args):
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
    for result in upload_resources(server, _deidentify_and_yield(), token=token, timeout=timeout):
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
    print(f"{status} Pushed {total} resource(s) to {server} — {errors} error(s)")


def _add_export_args(p):
    """Add export-subcommand arguments to an argparse parser."""
    p.add_argument("--server",
                   help="FHIR base URL (overrides FHIR_SOURCE_URL env). "
                        "E.g. http://host:8080/fhir")
    p.add_argument("--level", choices=["system", "type"], default="system",
                   help="Export level: 'system' for /$export, 'type' for /{Type}/$export.")
    p.add_argument("--resource-type", dest="resource_type",
                   help="Resource type for type-level export. "
                        "For system-level, sets the _type filter parameter.")
    p.add_argument("--type-filter", dest="type_filter",
                   help="Comma-separated _type filter for system-level export "
                        "(e.g. Patient,Observation). Overrides --resource-type for system level.")
    p.add_argument("--since",
                   help="Only export resources modified after this instant (sets _since param).")
    p.add_argument("--output", required=True,
                   help="Output NDJSON file path.")
    p.add_argument("--config", "-c", dest="config_filename",
                   help="YAML config file for anonymization rules.")
    p.add_argument("--token", dest="fhir_token",
                   help="Bearer token for FHIR server auth (overrides FHIR_SOURCE_TOKEN env).")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="HTTP timeout per request in seconds.")
    return p


def _run_export(args):
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
            fout.write(json.dumps(resource, separators=(',', ':')))
            fout.write("\n")
            total += 1
            if total % 100 == 0:
                print(f"  processed {total} resources...")

    print(f":thumbs_up: Bulk export: {total} resource(s) → {output_path}")


def _add_cohort_args(p):
    """Add cohort-subcommand arguments to an argparse parser."""
    p.add_argument("--server",
                   help="FHIR base URL (overrides FHIR_SOURCE_URL env).")
    p.add_argument("--search-type", dest="search_type", default="Condition",
                   help="Resource type to search for cohort selection (default: Condition).")
    p.add_argument("--code", required=True,
                   help="Code to search for, e.g. 'E11' or 'http://hl7.org/fhir/sid/icd-10|E11'.")
    p.add_argument("--search-params", dest="search_extra_params",
                   help="Additional search params, e.g. 'clinical-status=active&verification-status=confirmed'.")
    p.add_argument("--output", required=True,
                   help="Output NDJSON file path.")
    p.add_argument("--config", "-c", dest="config_filename",
                   help="YAML config file for anonymization rules.")
    p.add_argument("--token", dest="fhir_token",
                   help="Bearer token for FHIR server auth (overrides FHIR_SOURCE_TOKEN env).")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="HTTP timeout per request in seconds.")
    return p


def _run_cohort(args):
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
            server, args.search_type, search_params,
            token=token, timeout=timeout,
        ):
            if settings is not None:
                resource = process_data(resource, settings)
            fout.write(json.dumps(resource, separators=(',', ':')))
            fout.write("\n")
            total += 1
            if total % 100 == 0:
                print(f"  processed {total} resources...")

    print(f":thumbs_up: Cohort export: {total} resource(s) → {output_path}")


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Black-box FHIR processor for JSON, NDJSON, XML, and Bundles.",
    )
    parser.add_argument("input_file", nargs='?', help="Input FHIR file (JSON, NDJSON, or XML).")
    parser.add_argument("output_file", nargs='?', help="Output file (JSON, NDJSON, or XML).")
    parser.add_argument("--config", "-c", dest="config_filename",
                        help="YAML config file. If omitted, gPAS options must be provided.")
    parser.add_argument("--format", default="auto", choices=["auto", "json", "ndjson", "xml"],
                        help="Set both input and output format.")
    parser.add_argument("--input-format", default="auto", choices=["auto", "json", "ndjson", "xml"],
                        help="Input format override.")
    parser.add_argument("--output-format", default="auto", choices=["auto", "json", "ndjson", "xml"],
                        help="Output format override.")
    parser.add_argument("--strip-line-prefix", default="//",
                        help="Prefix to strip from NDJSON lines before parsing.")
    parser.add_argument("--match", default="Patient.id",
                        help="FHIRPath match used when building an in-memory gPAS rule.")
    parser.add_argument("--action", default="gpas_pseudonymize",
                        help="Action used when building an in-memory rule.")
    parser.add_argument("--gpas-url", help="gPAS base URL.")
    parser.add_argument("--gpas-admin-url", help="Optional gPAS admin UI URL used for domain discovery.")
    parser.add_argument("--gpas-domain", help="gPAS domain name.")
    parser.add_argument("--gpas-operation", default="pseudonymizeAllowCreate",
                        help="gPAS operation: pseudonymizeAllowCreate, pseudonymize, or dePseudonymize.")
    parser.add_argument("--gpas-token", help="Optional gPAS bearer token.")
    parser.add_argument("--gpas-timeout-sec", type=float, default=30.0,
                        help="gPAS timeout in seconds.")
    parser.add_argument("--gpas-list-domains", action="store_true",
                        help="List available gPAS domains and exit.")
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "process":
        argv = argv[1:]

    # Route fetch subcommand before falling through to legacy process parser
    if argv and argv[0] == "fetch":
        fetch_parser = argparse.ArgumentParser(prog="cli.py fetch")
        _add_fetch_args(fetch_parser)
        fetch_args = fetch_parser.parse_args(argv[1:])
        _run_fetch(fetch_args)
        return

    if argv and argv[0] == "everything":
        everything_parser = argparse.ArgumentParser(
            prog="cli.py everything",
            description="Fetch all resources via FHIR $everything, optionally de-identify, and save as NDJSON.",
        )
        _add_everything_args(everything_parser)
        everything_args = everything_parser.parse_args(argv[1:])
        _run_everything(everything_args)
        return

    if argv and argv[0] == "push":
        push_parser = argparse.ArgumentParser(
            prog="cli.py push",
            description="De-identify a local FHIR file and upload resources to a FHIR server.",
        )
        _add_push_args(push_parser)
        push_args = push_parser.parse_args(argv[1:])
        _run_push(push_args)
        return

    if argv and argv[0] == "export":
        export_parser = argparse.ArgumentParser(
            prog="cli.py export",
            description="Bulk export FHIR resources via $export, optionally de-identify, and save as NDJSON.",
        )
        _add_export_args(export_parser)
        export_args = export_parser.parse_args(argv[1:])
        _run_export(export_args)
        return

    if argv and argv[0] == "cohort":
        cohort_parser = argparse.ArgumentParser(
            prog="cli.py cohort",
            description="Export all records for patients matching a condition code via $everything.",
        )
        _add_cohort_args(cohort_parser)
        cohort_args = cohort_parser.parse_args(argv[1:])
        _run_cohort(cohort_args)
        return

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.gpas_list_domains:
        if not args.gpas_url and not args.gpas_admin_url:
            parser.error("--gpas-list-domains requires --gpas-url or --gpas-admin-url")
        domain_params = {
            'gpas_timeout_sec': args.gpas_timeout_sec,
        }
        if args.gpas_url:
            domain_params['gpas_url'] = args.gpas_url
        if args.gpas_admin_url:
            domain_params['gpas_admin_url'] = args.gpas_admin_url
        if args.gpas_token:
            domain_params['gpas_token'] = args.gpas_token

        for domain in list_gpas_domains(domain_params):
            print(domain)
        return

    if not args.input_file or not args.output_file:
        parser.error("input_file and output_file are required unless --gpas-list-domains is used")

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
    input_format_req = args.input_format if args.input_format != 'auto' else args.format
    output_format_req = args.output_format if args.output_format != 'auto' else args.format
    chosen_input_format = detect_format(args.input_file, input_format_req)
    chosen_output_format = detect_format(args.output_file, output_format_req)

    input_path = Path(args.input_file)
    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Stream NDJSON line-by-line to avoid loading entire file into memory (Step 6)
    if chosen_input_format == 'ndjson':
        total = 0
        with open(input_path, 'r', encoding='utf-8-sig') as fin, \
             open(output_path, 'w', encoding='utf-8') as fout:
            for raw_line in fin:
                line = raw_line.strip()
                if not line:
                    continue
                if args.strip_line_prefix and line.startswith(args.strip_line_prefix):
                    line = line[len(args.strip_line_prefix):]
                resource = json.loads(line)
                result = process_data(resource, settings)
                fout.write(json.dumps(result, separators=(',', ':')))
                fout.write('\n')
                total += 1
                if total % 100 == 0:
                    print(f"  processed {total} resources...")
    else:
        resource = read_input_file(str(input_path), chosen_input_format, args.strip_line_prefix)
        ret = process_data(resource, settings)
        write_output_file(ret, str(output_path), chosen_output_format)

    print(f":thumbs_up: Wrote output file {output_path}")


if __name__ == "__main__":
    main()
