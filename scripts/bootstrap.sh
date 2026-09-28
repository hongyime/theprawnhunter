#!/usr/bin/env bash
#
# One-shot setup of The Prawn Hunter on a fresh machine (macOS / Linux).
#
#   ./scripts/bootstrap.sh [VOLUME_BACKUP_DIR]
#
# With a backup dir, your data volumes (Telethon sessions etc.) are restored
# first. Without one, the stack starts fresh — add accounts later with
# /starthunter in Telegram. Supabase is external, so the DB comes along via
# the same SUPABASE_* creds in your .env.
set -euo pipefail
cd "$(dirname "$0")/.."

_env() { [ -f .env ] && (grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"') || true; }

echo "== 1/5 Prerequisites =="
command -v docker >/dev/null || { echo "docker not found on PATH"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 not found (need Docker with the compose plugin)"; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon not reachable — is Docker running?"; exit 1; }
echo "  ok"

echo "== 2/5 .env =="
if [ ! -f .env ]; then
  cat >&2 <<'EOF'
  .env is missing — it is gitignored and does NOT clone.
  Copy the real .env from your source machine, or start from the template:
      cp .env.template .env   # then fill in every secret
EOF
  exit 1
fi
echo "  found .env"

echo "== 3/5 External volumes =="
REDIS_VOL="$(_env REDIS_VOLUME_NAME)";        REDIS_VOL="${REDIS_VOL:-telegramhunter_redis_data}"
SESS_VOL="$(_env SESSIONS_VOLUME_NAME)";      SESS_VOL="${SESS_VOL:-telegramhunter_sessions}"
IMP_VOL="$(_env IMPORTS_VOLUME_NAME)";        IMP_VOL="${IMP_VOL:-telegramhunter_imports}"
BEAT_VOL="$(_env BEAT_SCHEDULE_VOLUME_NAME)"; BEAT_VOL="${BEAT_VOL:-telegramhunter_beat_schedule}"
for v in "$REDIS_VOL" "$SESS_VOL" "$IMP_VOL" "$BEAT_VOL"; do
  docker volume create "$v" >/dev/null && echo "  ok: $v"
done

if [ -n "${1:-}" ]; then
  echo "== 3b/5 Restore volume backups from '$1' =="
  bash "$(dirname "$0")/volumes.sh" restore "$1"
fi

echo "== 4/5 Build + start core (redis, api, worker, beat) =="
docker compose up -d --build

echo "== 5/5 Verify API health =="
PORT="$(_env API_PORT)"; PORT="${PORT:-8011}"
ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://localhost:${PORT}/health/" >/dev/null 2>&1; then ok=1; break; fi
  sleep 5
done
if [ "$ok" = 1 ]; then
  echo "  API healthy: http://localhost:${PORT}/health/"
else
  echo "  API not healthy yet — check: docker compose logs -f api"
fi

cat <<EOF

Done. Core stack is up.
  Status:        docker compose ps
  Add accounts:  /starthunter in Telegram (if you did not restore a sessions volume)
  Extras:        docker compose --profile full up -d        (bot, flower, frontend)
                 docker compose --profile honeypot up -d    (Cloudflare Tunnel)
EOF
