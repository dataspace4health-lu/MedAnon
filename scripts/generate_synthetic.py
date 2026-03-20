#!/usr/bin/env python3
"""CLI: generate synthetic FHIR Patient resources from a de-identified dataset.

Reads a de-identified FHIR file (NDJSON, JSON Bundle, or single JSON resource),
extracts statistical distributions from Patient resources, and outputs a
configurable number of synthetic Patient records as NDJSON.

Usage
-----
    python3 scripts/generate_synthetic.py \\
        --input  services/anonymizer/tests/data/TestBase/Patient.000.ndjson \\
        --count  100 \\
        --seed   42 \\
        --output /tmp/synthetic.ndjson

Options
-------
    --input   PATH     Input file (NDJSON, JSON, or XML). Required.
    --output  PATH     Output NDJSON file. Prints to stdout if omitted.
    --count   INT      Number of synthetic patients to generate (default: 100).
    --seed    INT      Random seed for reproducibility (optional).
    --format  STR      Input format: auto | ndjson | json | xml (default: auto).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running from the repo root without installing the package
_SRC = Path(__file__).parent.parent / "services" / "anonymizer" / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from analytics.synthetic import generate_synthetic_patients  # noqa: E402
from pipeline.io_formats import detect_format, read_input_file  # noqa: E402


def _unwrap_patients(payload) -> list[dict]:
    """Extract Patient resources from any parsed FHIR payload."""
    if isinstance(payload, list):
        resources = [r for r in payload if isinstance(r, dict)]
    elif isinstance(payload, dict):
        if payload.get("resourceType") == "Bundle":
            resources = [
                entry["resource"]
                for entry in payload.get("entry", [])
                if isinstance(entry.get("resource"), dict)
            ]
        else:
            resources = [payload]
    else:
        resources = []
    return [r for r in resources if r.get("resourceType") == "Patient"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate synthetic FHIR Patient resources from a de-identified dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--input", required=True, metavar="PATH",
                        help="Input file (NDJSON, JSON Bundle, or XML)")
    parser.add_argument("--output", default=None, metavar="PATH",
                        help="Output NDJSON file (stdout if omitted)")
    parser.add_argument("--count", type=int, default=100, metavar="N",
                        help="Number of synthetic patients to generate (default: 100)")
    parser.add_argument("--seed", type=int, default=None, metavar="INT",
                        help="Random seed for reproducibility")
    parser.add_argument("--format", default="auto",
                        choices=["auto", "ndjson", "json", "xml"],
                        dest="fmt",
                        help="Input format (default: auto-detect from extension)")
    args = parser.parse_args()

    # Parse input
    fmt = detect_format(args.input, args.fmt)
    payload = read_input_file(args.input, fmt)
    patients = _unwrap_patients(payload)

    if not patients:
        print(f"ERROR: no Patient resources found in {args.input}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Loaded {len(patients)} Patient resource(s) from {args.input}",
        file=sys.stderr,
    )

    # Generate synthetic records
    synthetic = generate_synthetic_patients(patients, count=args.count, seed=args.seed)

    # Serialise as NDJSON
    ndjson_lines = [json.dumps(p, ensure_ascii=False) for p in synthetic]
    output_text = "\n".join(ndjson_lines) + "\n"

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
        print(
            f"Generated {len(synthetic)} synthetic Patient(s) → {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(output_text)
        print(
            f"Generated {len(synthetic)} synthetic Patient(s) (stdout)",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
