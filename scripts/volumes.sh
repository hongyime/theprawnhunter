#!/usr/bin/env bash
#
# Backup / restore The Prawn Hunter's Docker data volumes:
#   redis_data, sessions, imports, beat_schedule
#
# Usage:
#   ./scripts/volumes.sh backup  [DIR]    # default DIR=./volume-backups
#   ./scripts/volumes.sh restore [DIR]    # default DIR=./volume-backups
#
# Volume names are read from .env (REDIS_VOLUME_NAME etc.) when present,
# otherwise fall back to the legacy telegramhunter_* defaults used by
# docker-compose.yml. Uses a throwaway alpine container + tar, so it behaves
# identically on macOS / Linux / Windows (Docker Desktop or Engine).
set -euo pipefail
cd "$(dirname "$0")/.."

_env() { [ -f .env ] && (grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"') || true; }
REDIS_VOL="$(_env REDIS_VOLUME_NAME)";        REDIS_VOL="${REDIS_VOL:-telegramhunter_redis_data}"
SESS_VOL="$(_env SESSIONS_VOLUME_NAME)";      SESS_VOL="${SESS_VOL:-telegramhunter_sessions}"
IMP_VOL="$(_env IMPORTS_VOLUME_NAME)";        IMP_VOL="${IMP_VOL:-telegramhunter_imports}"
BEAT_VOL="$(_env BEAT_SCHEDULE_VOLUME_NAME)"; BEAT_VOL="${BEAT_VOL:-telegramhunter_beat_schedule}"
VOLUMES=("$REDIS_VOL" "$SESS_VOL" "$IMP_VOL" "$BEAT_VOL")

ACTION="${1:-}"
DIR="${2:-./volume-backups}"

die() { echo "ERROR: $*" >&2; exit 1; }
command -v docker >/dev/null || die "docker not found on PATH"

case "$ACTION" in
  backup)
    mkdir -p "$DIR"; ABS="$(cd "$DIR" && pwd)"
    echo "Backing up volumes -> $ABS"
    for v in "${VOLUMES[@]}"; do
      if ! docker volume inspect "$v" >/dev/null 2>&1; then
        echo "  skip (not found): $v"; continue
      fi
      echo "  backup: $v"
      docker run --rm \
        --mount "type=volume,source=$v,target=/data,readonly" \
        --mount "type=bind,source=$ABS,target=/backup" \
        alpine tar czf "/backup/$v.tar.gz" -C /data . \
        || die "backup failed for $v"
    done
    echo "Done. Copy '$ABS' AND your .env to the target machine (both hold secrets)."
    ;;
  restore)
    ABS="$(cd "$DIR" 2>/dev/null && pwd)" || die "backup dir not found: $DIR"
    echo "Restoring volumes <- $ABS"
    for v in "${VOLUMES[@]}"; do
      f="$ABS/$v.tar.gz"
      [ -f "$f" ] || { echo "  skip (no archive): $v"; continue; }
      docker volume create "$v" >/dev/null
      echo "  restore: $v"
      docker run --rm \
        --mount "type=volume,source=$v,target=/data" \
        --mount "type=bind,source=$ABS,target=/backup" \
        alpine tar xzf "/backup/$v.tar.gz" -C /data \
        || die "restore failed for $v"
    done
    echo "Done."
    ;;
  *)
    die "usage: $0 {backup|restore} [DIR]"
    ;;
esac
