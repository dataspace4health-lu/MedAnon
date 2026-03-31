#!/usr/bin/env bash
# backup_gpas.sh — encrypted backup of the gPAS MySQL pseudonym database.
#
# USAGE
#   ./scripts/backup_gpas.sh [--restore <backup-file>]
#
# PREREQUISITES
#   - Docker + docker compose must be running (gpas-mysql container up).
#   - BACKUP_PASSPHRASE environment variable must be set (min 32 chars recommended).
#     Generate once: openssl rand -base64 48
#   - Optional: set BACKUP_DIR to override the default backup destination.
#
# BACKUP BEHAVIOUR
#   1. Dumps all gPAS databases from the running gpas-mysql container via mysqldump.
#   2. Compresses the dump with gzip.
#   3. Encrypts with AES-256-CBC (PBKDF2, 600 000 iterations) using BACKUP_PASSPHRASE.
#   4. Writes a timestamped .sql.gz.enc file to BACKUP_DIR (default: ./backups/gpas/).
#   5. Removes backups older than BACKUP_RETENTION_DAYS (default: 30).
#
# RESTORE BEHAVIOUR
#   ./scripts/backup_gpas.sh --restore ./backups/gpas/gpas_20260330_020000.sql.gz.enc
#   Decrypts → decompresses → replays SQL into the running gpas-mysql container.
#   WARNING: this replaces ALL existing pseudonym mappings. Ensure gPAS (WildFly) is
#   stopped before restoring to avoid in-flight pseudonymization conflicts.
#
# SCHEDULING (cron example — daily at 02:00)
#   0 2 * * * cd /opt/privacy-toolkit && BACKUP_PASSPHRASE="$(cat /run/secrets/gpas_backup_pass)" ./scripts/backup_gpas.sh >> /var/log/gpas_backup.log 2>&1
#
# SECURITY NOTES
#   - BACKUP_PASSPHRASE must never be stored in .env or committed to source control.
#     Use a secrets manager (Docker secrets, HashiCorp Vault, AWS Secrets Manager).
#   - Backup files contain all pseudonym mappings — treat them as sensitive data.
#   - Transfer backups to off-site encrypted storage (S3 SSE, Azure Blob, etc.).
#   - Test restores regularly in a staging environment.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONTAINER_NAME="${GPAS_MYSQL_CONTAINER:-gpas-mysql}"
MYSQL_ROOT_PASSWORD="${GPAS_MYSQL_ROOT_PASSWORD:?GPAS_MYSQL_ROOT_PASSWORD must be set}"
BACKUP_PASSPHRASE="${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set (min 32 chars)}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$0")/../backups/gpas}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/gpas_${TIMESTAMP}.sql.gz.enc"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
die() { echo "[ERROR] $*" >&2; exit 1; }

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "'$1' is not installed or not on PATH"
}

# ---------------------------------------------------------------------------
# Restore path
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--restore" ]]; then
    RESTORE_FILE="${2:?Usage: $0 --restore <backup-file>}"
    [[ -f "$RESTORE_FILE" ]] || die "Restore file not found: $RESTORE_FILE"

    require_cmd docker
    require_cmd openssl

    log "Starting restore from: $RESTORE_FILE"
    log "WARNING: this will REPLACE all existing gPAS pseudonym mappings."
    read -r -p "Type 'yes' to confirm: " CONFIRM
    [[ "$CONFIRM" == "yes" ]] || die "Restore cancelled."

    log "Decrypting and decompressing backup..."
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
        -pass "env:BACKUP_PASSPHRASE" \
        -in "$RESTORE_FILE" \
        | gunzip \
        | docker exec -i "$CONTAINER_NAME" \
            mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" \
        && log "Restore complete." \
        || die "Restore failed."
    exit 0
fi

# ---------------------------------------------------------------------------
# Backup path
# ---------------------------------------------------------------------------
require_cmd docker
require_cmd openssl
require_cmd gzip

# Verify the container is running
docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null \
    | grep -q "running" || die "Container '$CONTAINER_NAME' is not running."

mkdir -p "$BACKUP_DIR"

log "Starting gPAS MySQL backup → $BACKUP_FILE"

# Dump all databases (excludes information_schema, performance_schema, sys)
# --single-transaction: consistent InnoDB snapshot without locking tables
# --routines + --events: include stored procedures and scheduled events
docker exec "$CONTAINER_NAME" \
    mysqldump \
        -uroot -p"${MYSQL_ROOT_PASSWORD}" \
        --single-transaction \
        --routines \
        --events \
        --all-databases \
        --ignore-table=mysql.innodb_table_stats \
        --ignore-table=mysql.innodb_index_stats \
    | gzip \
    | openssl enc -aes-256-cbc -pbkdf2 -iter 600000 \
        -pass "env:BACKUP_PASSPHRASE" \
        -out "$BACKUP_FILE"

BACKUP_SIZE="$(du -sh "$BACKUP_FILE" | cut -f1)"
log "Backup complete: $BACKUP_FILE ($BACKUP_SIZE)"

# ---------------------------------------------------------------------------
# Retention: remove backups older than BACKUP_RETENTION_DAYS
# ---------------------------------------------------------------------------
PRUNED=0
while IFS= read -r -d '' old_file; do
    rm -f "$old_file"
    log "Pruned old backup: $old_file"
    PRUNED=$((PRUNED + 1))
done < <(find "$BACKUP_DIR" -name "gpas_*.sql.gz.enc" -mtime +"$BACKUP_RETENTION_DAYS" -print0)

[[ "$PRUNED" -gt 0 ]] && log "Pruned $PRUNED backup(s) older than ${BACKUP_RETENTION_DAYS} days."

log "Done."
