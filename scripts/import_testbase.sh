#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# MedAnon — import TestBase NDJSON files into HAPI FHIR
# Usage: bash scripts/import_testbase.sh [data/TestBase]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

FHIR_URL="${FHIR_SOURCE_URL:-http://localhost:8081/fhir}"
DATA_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/services/anonymizer/tests/data/TestBase}"
BATCH_SIZE=300

echo "FHIR server : ${FHIR_URL}"
echo "Data dir    : ${DATA_DIR}"
echo "Batch size  : ${BATCH_SIZE}"
echo ""

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
    if not hasattr(_local, "conn"):
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

# Build tier → file mapping
all_files = {
    os.path.basename(p).split(".")[0]: p
    for p in glob.glob(os.path.join(data_dir, "*.ndjson"))
    if "log" not in os.path.basename(p)
}
if not all_files:
    print(f"No .ndjson files found in {data_dir}")
    sys.exit(1)

grand_total = grand_errors = 0

for tier in LOAD_TIERS:
    tier_files = [all_files[t] for t in tier if t in all_files]
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
    [p for name, p in all_files.items() if name not in known],
    key=lambda p: os.path.basename(p),
)
for path in extra_files:
    u, e = upload_file(path)
    grand_total += u
    grand_errors += e

print()
print(f"Done — {grand_total} resources uploaded, {grand_errors} errors")
PYEOF
