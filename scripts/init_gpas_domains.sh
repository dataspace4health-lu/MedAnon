#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# init_gpas_domains.sh — Import gPAS domain template into a running stack
#
# Reads services/gpas/config/domains.json, applies all domains + parent-child
# relationships to MySQL, then restarts the gpas container so the in-memory
# cache picks up the new domains.
#
# Usage:
#   bash scripts/init_gpas_domains.sh
#   make init-domains
#
# Requirements: docker, python3, jq (optional — python3 fallback used)
# Run from the repo root.  Sources .env for GPAS_MYSQL_ROOT_PASSWORD.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOMAINS_JSON="${REPO_ROOT}/services/gpas/config/domains.json"
SQL_FILE="${REPO_ROOT}/services/gpas/sqls/03_init_gpas_domain.sql"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "${REPO_ROOT}/.env"
    set +o allexport
fi
MYSQL_ROOT_PASS="${GPAS_MYSQL_ROOT_PASSWORD:-root}"

# ── Pre-flight checks ─────────────────────────────────────────────────────────
echo "==> Checking required containers..."

for container in gpas-mysql gpas-wildfly; do
    if ! docker ps --format '{{.Names}}' | grep -q "^${container}$"; then
        echo "ERROR: Container '${container}' is not running. Start the stack first: make up"
        exit 1
    fi
done
echo "    gpas-mysql and gpas-wildfly are running."

if [[ ! -f "${DOMAINS_JSON}" ]]; then
    echo "ERROR: ${DOMAINS_JSON} not found."
    exit 1
fi

# ── Helper: run mysql without exposing password on the command line ────────────
# Writing credentials to a temp file avoids the password appearing in `ps`
# output, shell history, and Docker logs.
_mysql_exec() {
    local sql_or_file="$1"
    local use_stdin="${2:-no}"
    local cnf
    cnf=$(mktemp)
    chmod 0600 "${cnf}"
    printf '[client]\npassword=%s\n' "${MYSQL_ROOT_PASS}" > "${cnf}"
    if [[ "${use_stdin}" == "stdin" ]]; then
        docker exec -i gpas-mysql mysql --defaults-extra-file=/dev/stdin \
            -u root gpas < <(cat "${cnf}" -) <<< "" 2>/dev/null || true
        # Pipe via process substitution is complex in Docker — fall back to file copy
        docker cp "${cnf}" gpas-mysql:/tmp/.my_init.cnf
        docker exec -i gpas-mysql mysql --defaults-extra-file=/tmp/.my_init.cnf \
            -u root gpas < "${sql_or_file}"
        docker exec gpas-mysql rm -f /tmp/.my_init.cnf
    else
        docker cp "${cnf}" gpas-mysql:/tmp/.my_init.cnf
        docker exec gpas-mysql mysql --defaults-extra-file=/tmp/.my_init.cnf \
            -u root gpas -e "${sql_or_file}"
        docker exec gpas-mysql rm -f /tmp/.my_init.cnf
    fi
    rm -f "${cnf}"
}

# ── Apply the pre-built SQL init file ─────────────────────────────────────────
echo "==> Applying domain SQL from 03_init_gpas_domain.sql..."
_mysql_exec "${SQL_FILE}" stdin

# ── Clear internal_anonymisation_domain so gPAS recreates it cleanly ──────────
# gPAS's AnonymDomainBean tries to INSERT this on every startup.
# If it already exists in the DB, the INSERT fails and the domain is never
# added to the in-memory domainLocks HashMap → NullPointerException on
# listDomains().  Deleting it here forces a clean recreation on restart.
echo "==> Clearing internal_anonymisation_domain for clean restart..."
_mysql_exec "DELETE FROM domain WHERE name='internal_anonymisation_domain';"

# ── Restart gPAS to reload the in-memory cache ────────────────────────────────
echo "==> Restarting gpas container to reload domain cache..."
docker compose -f "${REPO_ROOT}/docker-compose.yml" restart gpas

# ── Wait for gPAS to be healthy ───────────────────────────────────────────────
echo "==> Waiting for gPAS to become healthy (up to 120s)..."
TIMEOUT=120
ELAPSED=0
while true; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' gpas-wildfly 2>/dev/null || echo "unknown")
    if [[ "${STATUS}" == "healthy" ]]; then
        echo "    gPAS is healthy."
        break
    fi
    if [[ ${ELAPSED} -ge ${TIMEOUT} ]]; then
        echo "ERROR: gPAS did not become healthy within ${TIMEOUT}s. Check: docker compose logs gpas"
        exit 1
    fi
    printf "    [%3ds] status=%s — waiting...\r" "${ELAPSED}" "${STATUS}"
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

# ── Verify domains were loaded ────────────────────────────────────────────────
echo "==> Verifying domains in database..."
_mysql_exec "SELECT name, label FROM domain WHERE name NOT LIKE 'internal_%' ORDER BY name;"

echo ""
echo "Done. All SPE domains and TESTING domain are available in gPAS."
echo "Web UI: http://localhost:8080/gpas-web/ (login: admin@ths / ttp-tools)"
