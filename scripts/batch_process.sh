#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SRC_DIR="$ROOT_DIR/services/anonymizer/src"
DATA_DIR="$ROOT_DIR/services/anonymizer/tests/data"
TOOLS_DIR="$ROOT_DIR/services/anonymizer/tools"
OUT_DIR="$ROOT_DIR/output"
AN_DIR="$OUT_DIR/analytics"
PY="$ROOT_DIR/.venv/bin/python3"

mkdir -p "$OUT_DIR" "$AN_DIR"

cd "$SRC_DIR"

# 1) Process diabetes bundle (JSON -> JSON)
$PY -m cli.main process "$DATA_DIR/diabetes.json" "$OUT_DIR/diabetes.out.json" --config "$ROOT_DIR/services/anonymizer/config/config_gpas.yaml"

# 2) Process synthea-like patient bulk file (NDJSON -> NDJSON)
$PY -m cli.main process "$DATA_DIR/Patient.000.ndjson" "$OUT_DIR/Patient.000.out.ndjson" --config "$ROOT_DIR/services/anonymizer/config/config_gpas.yaml"

# 3) Analyze results
$PY "$TOOLS_DIR/analyze_results.py" \
  --input "$DATA_DIR/diabetes.json" \
  --output "$OUT_DIR/diabetes.out.json" \
  --report-json "$AN_DIR/diabetes.analytics.json" \
  --report-md "$AN_DIR/diabetes.analytics.md"

$PY "$TOOLS_DIR/analyze_results.py" \
  --input "$DATA_DIR/Patient.000.ndjson" \
  --output "$OUT_DIR/Patient.000.out.ndjson" \
  --input-format ndjson \
  --output-format ndjson \
  --strip-line-prefix "//" \
  --report-json "$AN_DIR/patient000.analytics.json" \
  --report-md "$AN_DIR/patient000.analytics.md"

# 4) Combined markdown summary
{
  echo "# Batch Run Summary"
  echo
  echo "- output folder: $OUT_DIR"
  echo "- analytics folder: $AN_DIR"
  echo
  echo "## diabetes.json"
  cat "$AN_DIR/diabetes.analytics.md"
  echo
  echo "## Patient.000.ndjson"
  cat "$AN_DIR/patient000.analytics.md"
} > "$AN_DIR/summary.md"

echo "Batch complete."
echo "Outputs: $OUT_DIR"
echo "Analytics: $AN_DIR"
