#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# MedAnon  interactive patient de-identification test
# Usage: bash scripts/test_patient.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
umask 0077   # All new files/dirs created by this script are owner-only (no world-read)

MEDANON_PORT="${MEDANON_PORT:-8000}"
MEDANON_URL="http://localhost:${MEDANON_PORT}"
MEDANON_CONFIG_PROFILE="${MEDANON_CONFIG_PROFILE:-structural}"
FHIR_SOURCE_URL="${FHIR_SOURCE_URL:-http://localhost:8081/fhir}"
# URL that MedAnon uses to reach HAPI FHIR from *inside* Docker.
# When MedAnon runs in the compose stack it can't use localhost  it must
# use the Docker service hostname. Override to http://localhost:8081/fhir
# only if running MedAnon directly on the host (outside Docker).
MEDANON_FHIR_URL="${MEDANON_FHIR_URL:-http://hapi-fhir:8080/fhir}"
DATA_DIR="$(cd "$(dirname "$0")/.." && pwd)/data"
OUTPUT_DIR="$(cd "$(dirname "$0")/.." && pwd)/output"
mkdir -p "$DATA_DIR" "$OUTPUT_DIR"

# ── 1. Ensure MedAnon container is running ────────────────────────────────────
if ! curl -sf "${MEDANON_URL}/health" > /dev/null 2>&1; then
  echo "MedAnon not reachable at ${MEDANON_URL}  starting container..."
  docker rm -f medanon-test 2>/dev/null || true
# Detect the Docker network where gPAS is running
  # Use the stable network name declared in docker-compose.yml.  Falls back to
  # inspecting the gateway container (which carries the gpas-lb network alias).
  GPAS_NET=$(docker network inspect medanon-processing-net --format '{{.Name}}' 2>/dev/null \
    || docker inspect medanon-gateway --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null \
    || echo "bridge")

  docker run -d --name medanon-test \
    -p "${MEDANON_PORT}:8000" \
    --network "${GPAS_NET}" \
    --add-host=host.docker.internal:host-gateway \
    --env-file "$(dirname "$0")/../.env" \
    medanon:latest
  echo -n "Waiting for startup"
  for i in $(seq 1 30); do
    sleep 1
    if curl -sf "${MEDANON_URL}/health" > /dev/null 2>&1; then
      echo " ready."
      break
    fi
    echo -n "."
  done
fi

# ── 2. Ask for patient ID ─────────────────────────────────────────────────────
echo ""
echo "FHIR source : ${FHIR_SOURCE_URL}"
echo "MedAnon     : ${MEDANON_URL}"
echo "Config      : ${MEDANON_CONFIG_PROFILE}"
echo ""
read -rp "Patient ID: " PATIENT_ID

if [[ -z "$PATIENT_ID" ]]; then
  echo "No patient ID entered. Exiting."
  exit 1
fi

# Sanitize patient ID  allow only alphanumeric, hyphens and underscores
# to prevent path traversal (e.g. ../../../etc/passwd) in file names.
SAFE_PATIENT_ID="${PATIENT_ID//[^a-zA-Z0-9_-]/}"
if [[ -z "$SAFE_PATIENT_ID" || "$SAFE_PATIENT_ID" != "$PATIENT_ID" ]]; then
  echo "ERROR: Patient ID '${PATIENT_ID}' contains invalid characters. Use only letters, digits, hyphens and underscores."
  exit 1
fi

RAW_FILE="${DATA_DIR}/${SAFE_PATIENT_ID}_everything_raw.ndjson"
OUT_FILE="${OUTPUT_DIR}/${SAFE_PATIENT_ID}_everything_deidentified.ndjson"

# ── 3. Fetch raw data from HAPI ───────────────────────────────────────────────
# Use mktemp to avoid TOCTOU race on a shared /tmp path.
RAW_TMP=$(mktemp)
trap 'rm -f "$RAW_TMP"' EXIT

echo ""
echo "Fetching raw data for Patient/${PATIENT_ID} ..."
HTTP_STATUS=$(curl -s -o "$RAW_TMP" -w "%{http_code}" \
  "${FHIR_SOURCE_URL}/Patient/${PATIENT_ID}/\$everything?_count=200" \
  -H "Accept: application/fhir+json")

if [[ "$HTTP_STATUS" != "200" ]]; then
  echo "Error: HAPI FHIR returned HTTP ${HTTP_STATUS} for Patient/${PATIENT_ID}"
  cat "$RAW_TMP"
  exit 1
fi

python3 -c "
import json, sys
bundle = json.load(open(sys.argv[1]))
resources = [e['resource'] for e in bundle.get('entry', []) if e.get('resource')]
for r in resources:
    print(json.dumps(r))
print(len(resources), 'resources', file=sys.stderr)
" "$RAW_TMP" > "$RAW_FILE"

RAW_COUNT=$(wc -l < "$RAW_FILE")
echo "  Saved ${RAW_COUNT} raw resources → ${RAW_FILE}"

# ── 4. De-identify via MedAnon ────────────────────────────────────────────────
# Build JSON payload via Python to safely handle any special characters in the
# URL or patient ID without requiring jq.
echo "De-identifying ..."
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({'server_url': sys.argv[1], 'resource_type': sys.argv[2], 'resource_id': sys.argv[3]}))
" "$MEDANON_FHIR_URL" "Patient" "$PATIENT_ID")
curl -sf -X POST "${MEDANON_URL}/process/everything?config_profile=${MEDANON_CONFIG_PROFILE}" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  > "$OUT_FILE"

OUT_COUNT=$(wc -l < "$OUT_FILE")
echo "  Saved ${OUT_COUNT} de-identified resources → ${OUT_FILE}"

# ── 5. Print summary ──────────────────────────────────────────────────────────
echo ""
echo "────────────────────────────────────────────────────────────────"
echo "  De-identified output for Patient/${PATIENT_ID}"
echo "────────────────────────────────────────────────────────────────"
python3 -c "
import json

with open('${OUT_FILE}') as f:
    resources = [json.loads(line) for line in f]

patient = next((r for r in resources if r['resourceType'] == 'Patient'), None)
if patient:
    print(f\"  Patient pseudonym : {patient['id']}\")
    print(f\"  Birth year        : {patient.get('birthDate', 'n/a')}\")
    print()

by_type = {}
for r in resources:
    by_type.setdefault(r['resourceType'], []).append(r)

for rt, rs in sorted(by_type.items()):
    if rt == 'Patient':
        continue
    print(f\"  {rt} ({len(rs)})\")
    for r in rs:
        ref = r.get('subject', r.get('patient', {})).get('reference', '')
        eff = r.get('effectiveDateTime', r.get('recordedDate', ''))
        code = ''
        codings = r.get('code', {}).get('coding', [])
        if codings:
            code = codings[0].get('display', '')
        val = ''
        vq = r.get('valueQuantity', {})
        if vq:
            val = f\"{vq.get('value','')} {vq.get('unit','')}\"
        parts = [f'id={r[\"id\"]}', ref]
        if eff: parts.append(f'eff={eff}')
        if code: parts.append(code[:40])
        if val: parts.append(val)
        print(f\"    {'  '.join(p for p in parts if p)}\")
"
echo "────────────────────────────────────────────────────────────────"
echo ""
