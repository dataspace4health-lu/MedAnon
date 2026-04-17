#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# MedAnon — import TestBase NDJSON files into HAPI FHIR
# Usage: bash scripts/import_testbase.sh [data/TestBase]
#
# The source FHIR server (hapi-fhir) has no host port in the default stack —
# identified patient data must not be reachable outside Docker in production.
#
# URL resolution order:
#   1. FHIR_SOURCE_URL env var (explicit override)
#   2. http://localhost:8081/fhir — works when dev override is active
#      (docker compose -f docker-compose.yml -f docker-compose.dev.yml up)
#   3. Auto proxy — spins up a temporary socat container on source-net,
#      forwards localhost:8081 → hapi-fhir:8080, tears it down on exit.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

FHIR_URL="${FHIR_SOURCE_URL:-http://localhost:8081/fhir}"
DATA_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/services/anonymizer/tests/data/TestBase100}"
BATCH_SIZE=300
PROXY_CONTAINER="fhir-source-proxy-import"
PROXY_STARTED=false

# ── Resolve FHIR URL ──────────────────────────────────────────────────────────
# Spin up a temporary socat proxy when port 8081 is not already reachable.
if [ -z "${FHIR_SOURCE_URL:-}" ] && ! curl -sf --max-time 3 "${FHIR_URL}/metadata" >/dev/null 2>&1; then
  # Remove any leftover proxy from a previous interrupted run
  docker rm -f "${PROXY_CONTAINER}" >/dev/null 2>&1 || true

  echo "Note: localhost:8081 not reachable — starting temporary proxy (localhost:8081 → hapi-fhir:8080)"
  echo "      The proxy is removed automatically when the import finishes."
  echo ""

  # Determine project network name (prefix varies with working dir)
  SOURCE_NET=$(docker network ls --format '{{.Name}}' | grep 'source-net' | head -1)
  if [ -z "$SOURCE_NET" ]; then
    echo "ERROR: source-net Docker network not found — is the stack running?"
    echo "  Start it with: make up"
    exit 1
  fi

  docker run -d --rm \
    --name "${PROXY_CONTAINER}" \
    --network "${SOURCE_NET}" \
    -p 8081:8080 \
    alpine/socat \
    TCP-LISTEN:8080,fork,reuseaddr TCP:hapi-fhir:8080 >/dev/null

  PROXY_STARTED=true

  # Ensure proxy is stopped when the script exits (success or failure)
  trap 'docker rm -f "${PROXY_CONTAINER}" >/dev/null 2>&1 || true' EXIT
fi

echo "FHIR server : ${FHIR_URL}"
echo "Data dir    : ${DATA_DIR}"
echo "Batch size  : ${BATCH_SIZE}"
echo ""

# Wait for FHIR server to accept connections (up to 60s)
printf "Waiting for FHIR server..."
for i in $(seq 1 30); do
  if curl -sf --max-time 3 "${FHIR_URL}/metadata" >/dev/null 2>&1; then
    echo " ready"
    break
  fi
  if [ "$i" -eq 30 ]; then
    echo " TIMEOUT — server not reachable at ${FHIR_URL}"
    echo ""
    echo "Troubleshooting:"
    echo "  • Stack running?    docker compose ps"
    echo "  • Custom URL:       FHIR_SOURCE_URL=http://myhost:8081/fhir bash scripts/import_testbase100.sh"
    exit 1
  fi
  printf "."
  sleep 2
done

# Upload all .ndjson files via batch bundles (parallel where load-order allows)
python3 - <<PYEOF
import json, sys, os, glob, threading
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

fhir_url = "${FHIR_URL}".rstrip("/")
data_dir = "${DATA_DIR}"
batch_size = ${BATCH_SIZE}

# Tiers define which resource types may be uploaded in parallel.
# All types within a tier are independent of each other; each tier
# must complete before the next begins (referential integrity).
LOAD_TIERS = [
    ["Organization"],
    ["Location", "Practitioner"],
    ["PractitionerRole", "Patient"],
    ["Encounter"],
    ["Condition", "AllergyIntolerance", "Immunization",
     "MedicationRequest", "Procedure", "Device"],
    ["Observation"],
    ["DiagnosticReport", "DocumentReference"],
]

# Flat order map for sorting unknown types to the end
_LOAD_ORDER = [t for tier in LOAD_TIERS for t in tier]

# One persistent connection per thread (keep-alive, no TLS overhead per request)
_local = threading.local()

def _get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        parsed = urlparse(fhir_url)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme == "https":
            _local.conn = HTTPSConnection(host, port or 443, timeout=120)
        else:
            _local.conn = HTTPConnection(host, port or 80, timeout=120)
    return _local.conn

def post_bundle(resources):
    entries = [
        {
            "resource": r,
            "request": {
                "method": "PUT" if r.get("id") else "POST",
                "url": f"{r['resourceType']}/{r['id']}" if r.get("id") else r["resourceType"],
            },
        }
        for r in resources
    ]
    # Use "batch" (not "transaction") — independent entry processing, no table locks,
    # no full-bundle ACID commit.  Load ordering already guarantees referential integrity.
    bundle = {"resourceType": "Bundle", "type": "batch", "entry": entries}
    body = json.dumps(bundle).encode()
    parsed = urlparse(fhir_url)
    path = (parsed.path or "") + "/"
    for attempt in range(3):
        try:
            conn = _get_conn()
            conn.request(
                "POST", path, body=body,
                headers={
                    "Content-Type": "application/fhir+json",
                    "Accept": "application/fhir+json",
                    "Content-Length": str(len(body)),
                    "Connection": "keep-alive",
                },
            )
            resp = conn.getresponse()
            data = resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {data[:300].decode('utf-8', errors='replace')}")
            return json.loads(data)
        except (ConnectionResetError, BrokenPipeError, OSError):
            # Connection dropped — force reconnect on next attempt
            _local.conn = None
            if attempt == 2:
                raise

VALID_ALLERGY_CATEGORIES = {"food", "medication", "environment", "biologic"}

def sanitize(resource):
    if resource.get("resourceType") == "AllergyIntolerance" and resource.get("category"):
        cat = resource["category"]
        if isinstance(cat, list):
            fixed = [c for c in cat if c in VALID_ALLERGY_CATEGORIES]
            resource["category"] = fixed if fixed else resource.pop("category") or []
            if not resource.get("category"):
                del resource["category"]
        elif cat not in VALID_ALLERGY_CATEGORIES:
            del resource["category"]
    return resource

_print_lock = threading.Lock()

def upload_file(path):
    filename = os.path.basename(path)
    with open(path, encoding="utf-8-sig") as f:
        lines = [l.strip() for l in f if l.strip()]
    resources = [sanitize(json.loads(l)) for l in lines]
    total = len(resources)
    uploaded = errors = 0
    for i in range(0, total, batch_size):
        batch = resources[i:i + batch_size]
        try:
            post_bundle(batch)
            uploaded += len(batch)
        except RuntimeError as e:
            errors += len(batch)
            with _print_lock:
                print(f"\n  ERROR {filename} batch {i//batch_size + 1}: {e}", file=sys.stderr)
        pct = int(uploaded / total * 100) if total else 100
        with _print_lock:
            print(f"\r  {filename}: {uploaded}/{total} ({pct}%) errors={errors}   ", end="", flush=True)
    with _print_lock:
        print()
    return uploaded, errors

# Build tier → file mapping (resource type → sorted list of paths)
# A resource type may span multiple numbered files (e.g. Observation.000, Observation.001).
# All files for the same type belong to the same tier and are uploaded together.
all_files: dict[str, list[str]] = {}
for p in sorted(glob.glob(os.path.join(data_dir, "*.ndjson"))):
    if "log" in os.path.basename(p):
        continue
    rtype = os.path.basename(p).split(".")[0]
    all_files.setdefault(rtype, []).append(p)

if not all_files:
    print(f"No .ndjson files found in {data_dir}")
    sys.exit(1)

grand_total = grand_errors = 0

for tier in LOAD_TIERS:
    # Flatten all files for every resource type in this tier
    tier_files = [p for t in tier if t in all_files for p in all_files[t]]
    if not tier_files:
        continue
    # Upload all files in this tier concurrently
    with ThreadPoolExecutor(max_workers=len(tier_files)) as pool:
        futures = {pool.submit(upload_file, p): p for p in tier_files}
        for fut in as_completed(futures):
            u, e = fut.result()
            grand_total += u
            grand_errors += e

# Any resource types not in LOAD_TIERS — upload sequentially at the end
known = set(_LOAD_ORDER)
extra_files = sorted(
    [p for rtype, paths in all_files.items() if rtype not in known for p in paths],
    key=lambda p: os.path.basename(p),
)
for path in extra_files:
    u, e = upload_file(path)
    grand_total += u
    grand_errors += e

print()
print(f"Done — {grand_total} resources uploaded, {grand_errors} errors")
PYEOF
