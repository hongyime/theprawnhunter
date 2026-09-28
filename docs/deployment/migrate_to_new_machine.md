# Move The Prawn Hunter to another machine

Two things do **not** travel with `git clone`:

1. **`.env`** — gitignored (holds all your secrets).
2. **Docker data volumes** — Telethon **sessions**, redis, imports, beat schedule.
   Session files are the important, stateful bit.

Supabase is external/managed, so the **database comes along for free** — the new
machine just uses the same `SUPABASE_*` creds from your `.env`.

These helpers move everything in one shot. They work on **macOS, Linux, and
Windows** (bash `.sh` / PowerShell `.ps1`).

---

## On the SOURCE machine — back up the data volumes

```bash
# macOS / Linux
./scripts/volumes.sh backup                 # -> ./volume-backups/*.tar.gz
```
```powershell
# Windows
.\scripts\volumes.ps1 backup                # -> .\volume-backups\*.tar.gz
```

Then copy **two things** to the new machine — **securely**, both contain secrets:

- your **`.env`** file
- the **`volume-backups/`** folder

(`scp`, a USB stick, an encrypted transfer — anything except git.)

---

## On the TARGET machine — one command

```bash
git clone <repo-url> theprawnhunter && cd theprawnhunter
#   put your copied .env in the repo root
#   put the copied volume-backups/ in the repo root

# macOS / Linux
./scripts/bootstrap.sh ./volume-backups
```
```powershell
# Windows
.\scripts\bootstrap.ps1 .\volume-backups
```

`bootstrap` does everything:
1. checks Docker + the compose plugin are present and the daemon is up,
2. verifies `.env` exists,
3. creates the four external volumes,
4. **restores your volume backups** (sessions etc.) from the given folder,
5. builds + starts the core stack (`redis`, `api`, `worker`, `beat`),
6. waits for the API health check.

### Start fresh instead (no session migration)

Omit the backup dir — then add accounts with `/starthunter` in Telegram:

```bash
./scripts/bootstrap.sh          #   .\scripts\bootstrap.ps1
```

---

## Just volumes, no full setup

```bash
./scripts/volumes.sh restore ./volume-backups     # .\scripts\volumes.ps1 restore .\volume-backups
```

---

## After bootstrap

```bash
docker compose ps                                 # all core services healthy?
docker compose --profile full up -d               # + bot, flower, frontend
docker compose --profile honeypot up -d           # + Cloudflare Tunnel (see cloudflare_tunnel.md)
```

## Notes & gotchas

- **Treat `volume-backups/telegramhunter_sessions.tar.gz` like a credential** —
  it contains live Telethon auth keys.
- Volume names are read from `.env` (`REDIS_VOLUME_NAME`, `SESSIONS_VOLUME_NAME`,
  …) so custom names are honored automatically; otherwise the `telegramhunter_*`
  defaults are used.
- If you did **not** migrate the sessions volume, the core pipeline still runs —
  only Telethon history/media archiving is unavailable until you `/starthunter`.
- Apple Silicon and x86 both build natively; no arch flags needed.
- The scripts use a throwaway `alpine` + `tar` container, so no host tar/gzip is
  required — just Docker.
