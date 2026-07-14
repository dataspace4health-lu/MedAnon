#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# init_gpas_domains.sh  Bootstrap gPAS database: users, permissions, domains
#
# Idempotent.  Safe to run on a fresh rebuild (volumes deleted) or against a
# running stack.  Covers everything docker-entrypoint-initdb.d would do on
# first boot PLUS the domain seed that must be applied after gPAS has started.
#
# What it does (in order):
#   1. Re-seeds gRAS schema + users so logins work after a volume wipe
#      - admin / ttp-tools  (admin: all gPAS domains + all roles)
#      - user  / ttp-tools  (standard: pseudonym mapping + dashboards)
#   2. Clears internal_anonymisation_domain so gPAS recreates it cleanly
#   3. Applies all SPE + TESTING domain definitions
#   4. Restarts gPAS to reload its in-memory domain cache
#   5. Verifies domains loaded correctly
#
# Usage:
#   bash scripts/init_gpas_domains.sh
#   make init-domains
#
# Requirements: docker
# Run from the repo root.  Sources .env for GPAS_DB_PASSWORD.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOMAINS_JSON="${REPO_ROOT}/services/gpas/config/domains.json"
SQL_FILE="${REPO_ROOT}/services/gpas/sqls_pg/03_init_gpas_domain.sql"

# ── Load .env ─────────────────────────────────────────────────────────────────
if [[ -f "${REPO_ROOT}/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1090
    source "${REPO_ROOT}/.env"
    set +o allexport
fi
PGPASSWORD="${GPAS_DB_PASSWORD:-gpas_password}"
PGUSER="${GPAS_DB_USER:-gpas_user}"

# ── Pre-flight checks ─────────────────────────────────────────────────────────
echo "==> Checking required containers..."

if ! docker ps --format '{{.Names}}' | grep -q "^gpas-postgres$"; then
    echo "ERROR: Container 'gpas-postgres' is not running. Start the stack first: make up"
    exit 1
fi

# gpas service has no fixed container_name (supports --scale); find it via compose.
GPAS_CONTAINER=$(docker compose -f "${REPO_ROOT}/docker-compose.yml" ps -q gpas 2>/dev/null | head -1)
if [[ -z "${GPAS_CONTAINER}" ]]; then
    echo "ERROR: No running 'gpas' containers found. Start the stack first: make up"
    exit 1
fi
echo "    gpas-postgres and gpas service (container ${GPAS_CONTAINER:0:12}) are running."

if [[ ! -f "${DOMAINS_JSON}" ]]; then
    echo "ERROR: ${DOMAINS_JSON} not found."
    exit 1
fi

# ── Helper: run psql inside the gpas-postgres container ─────────────────────
# Passes PGPASSWORD via env to avoid exposing credentials on the command line.
_psql_exec() {
    local sql_or_file="$1"
    local use_stdin="${2:-no}"
    if [[ "${use_stdin}" == "stdin" ]]; then
        docker exec -i gpas-postgres \
            env PGPASSWORD="${PGPASSWORD}" \
            psql -U "${PGUSER}" -d gpas -q < "${sql_or_file}"
    else
        docker exec gpas-postgres \
            env PGPASSWORD="${PGPASSWORD}" \
            psql -U "${PGUSER}" -d gpas -c "${sql_or_file}"
    fi
}

# ── Step 1: Re-seed gRAS schema + users (idempotent) ──────────────────────────
# docker-entrypoint-initdb.d only runs on a fresh volume.  After `docker
# compose down -v` + rebuild, users are gone.  We re-apply the schema and seed
# data here so that make init-domains is sufficient to fully restore a stack.
#
# Credentials after seeding:
#   admin@ths / ttp-tools   full admin access to all gPAS domains + web UI
#   user@ths  / ttp-tools   pseudonym mapping + dashboard (no admin actions)
GRAS_SQL="${REPO_ROOT}/services/gpas/sqls_pg/01_create_database_gras.sql"
GRAS_SEED="${REPO_ROOT}/services/gpas/sqls_pg/02_init_database_gras_for_gpas.sql"

if [[ ! -f "${GRAS_SQL}" || ! -f "${GRAS_SEED}" ]]; then
    echo "ERROR: gRAS SQL files not found under services/gpas/sqls_pg/"
    exit 1
fi

echo "==> Seeding gRAS schema (idempotent  safe to re-run)..."
_psql_exec "${GRAS_SQL}" stdin

echo "==> Seeding gRAS users and permissions..."
_psql_exec "${GRAS_SEED}" stdin

# Verify users were created
USER_COUNT=$(docker exec gpas-postgres \
    env PGPASSWORD="${PGPASSWORD}" \
    psql -U "${PGUSER}" -d gpas -tAq \
    -c "SELECT COUNT(*) FROM gras.\"user\" WHERE name IN ('admin','user');")

if [[ "${USER_COUNT}" -lt 2 ]]; then
    echo "ERROR: gRAS users were not seeded correctly (found ${USER_COUNT}/2)."
    echo "       Check that pgcrypto extension is enabled in the gpas database."
    exit 1
fi
echo "    gRAS users seeded: admin@ths and user@ths (password: ttp-tools)."

# ── Step 2: Apply the pre-built domain SQL ────────────────────────────────────
echo "==> Applying domain SQL from 03_init_gpas_domain.sql..."
_psql_exec "${SQL_FILE}" stdin

# ── Step 3: Clear internal_anonymisation_domain so gPAS recreates it cleanly ──
# gPAS's AnonymDomainBean tries to INSERT this on every startup.
# If it already exists in the DB, the INSERT fails and the domain is never
# added to the in-memory domainLocks HashMap → NullPointerException on
# listDomains().  Deleting it here forces a clean recreation on restart.
echo "==> Clearing internal_anonymisation_domain for clean restart..."
_psql_exec "DELETE FROM domain WHERE name='internal_anonymisation_domain';"

# ── Step 4: Restart gPAS to reload the in-memory domain cache ────────────────
echo "==> Restarting gpas container to reload domain cache..."
docker compose -f "${REPO_ROOT}/docker-compose.yml" restart gpas

# ── Wait for gPAS to be healthy ───────────────────────────────────────────────
echo "==> Waiting for gPAS to become healthy (up to 120s)..."
TIMEOUT=120
ELAPSED=0
while true; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "${GPAS_CONTAINER}" 2>/dev/null || echo "unknown")
    if [[ "${STATUS}" == "healthy" ]]; then
        echo "    gPAS is healthy."
        break
    fi
    if [[ ${ELAPSED} -ge ${TIMEOUT} ]]; then
        echo "ERROR: gPAS did not become healthy within ${TIMEOUT}s. Check: docker compose logs gpas"
        exit 1
    fi
    printf "    [%3ds] status=%s  waiting...\r" "${ELAPSED}" "${STATUS}"
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

# ── Step 5: Verify domains were loaded ───────────────────────────────────────
echo "==> Verifying domains in database..."
_psql_exec "SELECT name, label FROM domain WHERE name NOT LIKE 'internal_%' ORDER BY name;"

echo ""
echo "Done. gPAS is fully initialised."
echo ""
echo "  Web UI:  http://localhost:8080/gpas-web/"
echo "  Logins:"
echo "    admin@ths / ttp-tools   full admin (all domains + web UI config)"
echo "    user@ths  / ttp-tools   standard   (pseudonym mapping + dashboards)"
