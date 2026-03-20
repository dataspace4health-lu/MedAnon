#!/usr/bin/env bash
# batch_fetch.sh — pull all resources from HAPI FHIR, anonymize, write NDJSON
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SCRIPT_DIR/../services/anonymizer/src"
CONFIG_DIR="$SCRIPT_DIR/../services/anonymizer/config"
OUTPUT_DIR="$SCRIPT_DIR/../output"

mkdir -p "$OUTPUT_DIR"

HAPI_URL="${HAPI_URL:-http://localhost:8081/fhir}"
OUTPUT_FILE="$OUTPUT_DIR/hapi_export.ndjson"
# Auto-select gPAS config when GPAS_URL is set (matches api/main.py logic)
if [ -n "${GPAS_URL:-}" ]; then
  CONFIG_FILE="${CONFIG_FILE:-$CONFIG_DIR/config_gpas.yaml}"
else
  CONFIG_FILE="${CONFIG_FILE:-$CONFIG_DIR/config.yaml}"
fi

echo "Fetching from $HAPI_URL → $OUTPUT_FILE"

cd "$SRC_DIR"
python3 -m cli.main fetch \
  --server "$HAPI_URL" \
  --output "$OUTPUT_FILE" \
  --config "$CONFIG_FILE"

echo "Done. Output: $OUTPUT_FILE"
