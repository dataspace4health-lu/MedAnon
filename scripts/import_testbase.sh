#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# MedAnon — import TestBase NDJSON files into HAPI FHIR
# Usage: bash scripts/import_testbase.sh [data/TestBase]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

FHIR_URL="${FHIR_SOURCE_URL:-http://localhost:8081/fhir}"
DATA_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)/services/anonymizer/tests/data/TestBase}"
BATCH_SIZE=400

echo "FHIR server : ${FHIR_URL}"
echo "Data dir    : ${DATA_DIR}"
echo "Batch size  : ${BATCH_SIZE}"
echo ""

# Upload all .ndjson files via transaction bundles
python3 - <<PYEOF
import json, sys, urllib.request, urllib.error, os, glob

fhir_url = "${FHIR_URL}".rstrip("/")
data_dir = "${DATA_DIR}"
batch_size = ${BATCH_SIZE}

# Load order: reference targets before referencing resources
# Observation must come before DiagnosticReport (DiagnosticReport.result refs)
LOAD_ORDER = [
    "Organization", "Location", "Practitioner", "PractitionerRole",
    "Patient", "Encounter", "Condition", "AllergyIntolerance",
    "Immunization", "MedicationRequest", "Procedure", "Device",
    "Observation", "DiagnosticReport", "DocumentReference",
]

def post_bundle(resources):
    entries = []
    for r in resources:
        rt = r.get("resourceType")
        rid = r.get("id")
        entries.append({
            "resource": r,
            "request": {
                "method": "PUT" if rid else "POST",
                "url": f"{rt}/{rid}" if rid else rt,
            }
        })
    bundle = {"resourceType": "Bundle", "type": "transaction", "entry": entries}
    body = json.dumps(bundle).encode()
    req = urllib.request.Request(
        url=f"{fhir_url}/",
        data=body,
        headers={"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:300]}") from e

VALID_ALLERGY_CATEGORIES = {"food", "medication", "environment", "biologic"}

def sanitize(resource):
    """Fix known FHIR R4 data quality issues before upload."""
    if resource.get("resourceType") == "AllergyIntolerance" and resource.get("category"):
        cat = resource["category"]
        if isinstance(cat, list):
            fixed = [c for c in cat if c in VALID_ALLERGY_CATEGORIES]
            if fixed:
                resource["category"] = fixed
            else:
                del resource["category"]
        elif cat not in VALID_ALLERGY_CATEGORIES:
            del resource["category"]
    return resource

def upload_file(path):
    filename = os.path.basename(path)
    with open(path, encoding="utf-8-sig") as f:
        lines = [l.strip() for l in f if l.strip()]
    resources = [sanitize(json.loads(l)) for l in lines]
    total = len(resources)
    uploaded = 0
    errors = 0
    for i in range(0, total, batch_size):
        batch = resources[i:i + batch_size]
        try:
            post_bundle(batch)
            uploaded += len(batch)
        except RuntimeError as e:
            errors += len(batch)
            print(f"  ERROR batch {i//batch_size + 1}: {e}", file=sys.stderr)
        pct = int(uploaded / total * 100) if total else 100
        print(f"\r  {filename}: {uploaded}/{total} ({pct}%) errors={errors}", end="", flush=True)
    print()
    return uploaded, errors

# Sort files by load order
all_files = glob.glob(os.path.join(data_dir, "*.ndjson"))
def sort_key(path):
    name = os.path.basename(path).split(".")[0]
    try:
        return LOAD_ORDER.index(name)
    except ValueError:
        return len(LOAD_ORDER)

sorted_files = sorted(all_files, key=sort_key)
if not sorted_files:
    print(f"No .ndjson files found in {data_dir}")
    sys.exit(1)

grand_total = grand_errors = 0
for path in sorted_files:
    if "log" in os.path.basename(path):
        continue
    u, e = upload_file(path)
    grand_total += u
    grand_errors += e

print()
print(f"Done — {grand_total} resources uploaded, {grand_errors} errors")
PYEOF
