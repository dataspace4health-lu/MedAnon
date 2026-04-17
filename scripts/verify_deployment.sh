#!/usr/bin/env bash
# verify_deployment.sh — post-startup smoke test for the MedAnon stack.
# Checks that every service is healthy and responding correctly.
# Usage: bash scripts/verify_deployment.sh
set -euo pipefail

# Source .env so we pick up MEDANON_API_KEY and port overrides.
# Use grep to skip comments and empty lines, then export.
if [ -f .env ]; then
    set -a
    eval "$(grep -v '^\s*#' .env | grep -v '^\s*$')"
    set +a
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

pass() { PASS=$((PASS+1)); printf "${GREEN}  [PASS]${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL+1)); printf "${RED}  [FAIL]${NC} %s\n" "$1"; }
warn() { WARN=$((WARN+1)); printf "${YELLOW}  [WARN]${NC} %s\n" "$1"; }

ANON_PORT="${ANONYMIZER_PORT:-8000}"
HAPI_PORT="${HAPI_PORT:-8081}"
GPAS_PORT="${GPAS_PORT:-8080}"
UI_PORT="${UI_PORT:-8501}"

echo ""
echo "=========================================="
echo "  MedAnon Deployment Verification"
echo "=========================================="
echo ""

# ── 1. Docker container health ──────────────────────────────────────────────
echo "1. Container health"
ALL_HEALTHY=true
for svc in medanon hapi-fhir hapi-fhir-target hapi-postgres hapi-target-postgres gpas-lb gpas-postgres medanon-redis medanon-ui; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$svc" 2>/dev/null || echo "not_found")
    case "$STATUS" in
        healthy)   pass "$svc: healthy" ;;
        starting)  warn "$svc: still starting (may need more time)"; ALL_HEALTHY=false ;;
        unhealthy) fail "$svc: unhealthy"; ALL_HEALTHY=false ;;
        *)         fail "$svc: container not found or no health check"; ALL_HEALTHY=false ;;
    esac
done
echo ""

# ── 2. Anonymizer API ───────────────────────────────────────────────────────
echo "2. Anonymizer API (port $ANON_PORT)"
# Health endpoint
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${ANON_PORT}/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/health -> 200"
else
    fail "/health -> $HTTP_CODE"
fi

# Ready endpoint
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${ANON_PORT}/ready" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/ready -> 200"
else
    warn "/ready -> $HTTP_CODE (dependencies may still be initialising)"
fi

# Metrics endpoint
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${ANON_PORT}/metrics" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/metrics -> 200"
else
    fail "/metrics -> $HTTP_CODE"
fi

# Process endpoint (simple Patient resource)
API_KEY="${MEDANON_API_KEY:-}"
AUTH_HEADER=""
if [ -n "$API_KEY" ]; then
    AUTH_HEADER="-H X-API-Key:${API_KEY}"
fi
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' \
    $AUTH_HEADER \
    -H "Content-Type: application/json" \
    -d '{"resourceType":"Patient","id":"smoke-test","name":[{"family":"Test"}]}' \
    "http://localhost:${ANON_PORT}/v1/process" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/process -> 200 (de-identification works)"
else
    fail "/process -> $HTTP_CODE"
fi
echo ""

# ── 3. HAPI FHIR source server ────────────────────────────────────────────
# The source FHIR server holds identified patient data and is intentionally
# NOT published to a host port.  Verify it via the anonymizer container
# which shares the internal Docker network.
echo "3. HAPI FHIR source server (internal only — no host port)"
HTTP_CODE=$(docker compose exec -T anonymizer python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://hapi-fhir:8080/fhir/metadata', timeout=5)
    print(r.status)
except Exception:
    print('000')
" 2>/dev/null | tr -d '[:space:]')
if [ "$HTTP_CODE" = "200" ]; then
    pass "/fhir/metadata -> 200 (via internal network)"
else
    # Fallback: maybe HAPI_PORT is exposed via override file
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${HAPI_PORT}/fhir/metadata" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        FHIR_VER=$(curl -s "http://localhost:${HAPI_PORT}/fhir/metadata" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('fhirVersion','?'))" 2>/dev/null || echo "?")
        pass "/fhir/metadata -> 200 (FHIR $FHIR_VER) (host port override)"
    else
        fail "/fhir/metadata -> unreachable (container may be down)"
    fi
fi

# Actuator health (used by Docker health check)
HTTP_CODE=$(docker compose exec -T anonymizer python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://hapi-fhir:8080/actuator/health', timeout=5)
    print(r.status)
except Exception:
    print('000')
" 2>/dev/null | tr -d '[:space:]')
if [ "$HTTP_CODE" = "200" ]; then
    pass "/actuator/health -> 200 (via internal network)"
else
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${HAPI_PORT}/actuator/health" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ]; then
        pass "/actuator/health -> 200 (host port override)"
    else
        fail "/actuator/health -> unreachable"
    fi
fi
echo ""

# ── 3b. HAPI FHIR target server (de-identified data) ─────────────────────
HAPI_TARGET_PORT="${HAPI_TARGET_PORT:-8082}"
echo "3b. HAPI FHIR target server (port $HAPI_TARGET_PORT)"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${HAPI_TARGET_PORT}/fhir/metadata" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    FHIR_VER=$(curl -s "http://localhost:${HAPI_TARGET_PORT}/fhir/metadata" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('fhirVersion','?'))" 2>/dev/null || echo "?")
    pass "/fhir/metadata -> 200 (FHIR $FHIR_VER)"
else
    fail "/fhir/metadata -> $HTTP_CODE"
fi

HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${HAPI_TARGET_PORT}/actuator/health" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/actuator/health -> 200"
else
    fail "/actuator/health -> $HTTP_CODE (health check will fail!)"
fi
echo ""

# ── 4. gPAS pseudonymisation service ───────────────────────────────────────
echo "4. gPAS (port $GPAS_PORT)"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${GPAS_PORT}/" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/ -> 200 (WildFly running)"
else
    fail "/ -> $HTTP_CODE"
fi

HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${GPAS_PORT}/ttp-fhir/fhir/gpas/metadata" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/ttp-fhir/fhir/gpas/metadata -> 200 (TTP-FHIR gateway up)"
else
    warn "/ttp-fhir/fhir/gpas/metadata -> $HTTP_CODE (gPAS domains may not be initialised yet)"
fi
echo ""

# ── 5. React UI ──────────────────────────────────────────────────────────────
echo "5. Web UI (port $UI_PORT)"
HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${UI_PORT}/healthz" 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ]; then
    pass "/healthz -> 200"
else
    warn "/healthz -> $HTTP_CODE (UI may still be starting)"
fi
echo ""

# ── 6. Java version compatibility check ─────────────────────────────────────
echo "6. Build compatibility"
HC_CLASS="services/fhir-server/healthcheck/HealthCheck.class"
if [ -f "$HC_CLASS" ]; then
    # Read class file major version (bytes 6-7)
    CLASS_VER=$(python3 -c "
with open('$HC_CLASS','rb') as f:
    d=f.read(8)
    print(int.from_bytes(d[6:8],'big'))
" 2>/dev/null || echo "0")
    CONTAINER_VER=$(docker exec hapi-fhir java -version 2>&1 | grep -oP 'version "\K[^"]+' | cut -d. -f1 || echo "0")
    EXPECTED_CLASS_VER=$((CONTAINER_VER + 44))
    if [ "$CLASS_VER" -le "$EXPECTED_CLASS_VER" ] 2>/dev/null; then
        pass "HealthCheck.class version $CLASS_VER compatible with Java $CONTAINER_VER (max $EXPECTED_CLASS_VER)"
    else
        fail "HealthCheck.class version $CLASS_VER > Java $CONTAINER_VER max ($EXPECTED_CLASS_VER). Run: make build-healthcheck"
    fi
else
    fail "HealthCheck.class not found. Run: make build-healthcheck"
fi
echo ""

# ── Summary ─────────────────────────────────────────────────────────────────
echo "=========================================="
printf "  Results: ${GREEN}%d passed${NC}" "$PASS"
if [ "$WARN" -gt 0 ]; then printf ", ${YELLOW}%d warnings${NC}" "$WARN"; fi
if [ "$FAIL" -gt 0 ]; then printf ", ${RED}%d failed${NC}" "$FAIL"; fi
echo ""
echo "=========================================="
echo ""

if [ "$FAIL" -gt 0 ]; then
    echo "Some checks failed. Run 'docker compose logs <service>' to investigate."
    exit 1
fi
exit 0
