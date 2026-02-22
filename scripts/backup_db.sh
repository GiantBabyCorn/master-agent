#!/usr/bin/env bash
set -euo pipefail

BACKUP_DIR="${1:-./backups}"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="$BACKUP_DIR/master-agent-$STAMP.sql"

DB_USER="${POSTGRES_USER:-postgres}"
DB_NAME="${POSTGRES_DB:-master_agent}"

docker compose exec -T master_agent_db pg_dump -U "$DB_USER" -d "$DB_NAME" > "$OUT_FILE"
echo "Backup written: $OUT_FILE"
