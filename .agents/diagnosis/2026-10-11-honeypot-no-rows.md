# Diagnosis: Honeypot receiver stores no updates

Date: 2026-10-11 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED — REMEDIATED

## Symptom Contract

- Expected: Telegram webhook updates reach the API and create rows in `honeypot_updates`.
- Observed: live Supabase count is 0.
- Scope: current running API/worker and the configured public hostname.
- Onset: unknown from available evidence.
- Repro: `GET https://winnethepooh.hong-yi.me/health/` returned HTTP 530 while `GET http://127.0.0.1:8011/health/` returned HTTP 200.

## Evidence

1. Live `honeypot_updates` exact count was 0, queried through the running API container.
2. API and worker settings both report honeypot mode enabled, receiver URL configured, secret configured, and allowlist `AUTO`.
3. The configured receiver uses HTTPS host `winnethepooh.hong-yi.me` and path `/honeypot`.
4. The public health endpoint returned HTTP 530; the local API health endpoint returned HTTP 200.
5. Initially, no Cloudflare Tunnel container or host process was running. The host `cloudflared` binary was installed.
6. Initially, the Compose tunnel token and host Cloudflare certificate were absent.
7. After Cloudflare authentication, the existing `prawnhunter` tunnel had no active connector. Its remote hostname route targeted `http://localhost:8011`.
8. Running the connector in Docker with that origin returned HTTP 502 because container-local `localhost` is not the API service.
9. The remote route now targets `http://api:8001`; the connector has registered, and the public health endpoint returns HTTP 200.
10. The tunnel token was accidentally printed by a diagnostic command. It was rotated, old tunnel connections were force-disconnected, and the replacement is stored in ignored `.cloudflared/tunnel.env` with mode 0600.
11. The shared `.env` is loaded by the API service. Compose now loads the tunnel-only file exclusively for `cloudflared`; resolved Compose config confirms the API no longer receives the tunnel token.
12. `app/api/routers/honeypot.py:138-146` inserts accepted payloads into Supabase; the current receiver path is `/receive/{credential_id}`. Legacy path-secret URLs are intentionally unsupported and the stale comments claiming compatibility were corrected.

## Root Cause

The public callback path was unavailable because no authenticated Cloudflare Tunnel connector was running. After starting the existing remote-managed tunnel, its configured origin also needed to match the connector's network namespace. The Docker connector must target `api:8001`; `localhost:8011` refers to the connector container itself.

## Why It Was Not Obvious

Honeypot feature flags and the database insert path were configured correctly. Those settings did not establish the public network path. The Compose service is behind the `honeypot` profile, and its token had not been configured. The existing Cloudflare route also retained a host-local origin while the connector was started in Docker.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Start the existing remotely managed Tunnel with its token and Docker-network origin | Store `TUNNEL_TOKEN` in `.cloudflared/tunnel.env`, set origin to `http://api:8001`, start the `honeypot` profile | Low; uses the existing tunnel and hostname | Applied |

## Verification Plan

Cloudflare reports registered tunnel connections, the public health endpoint returns 200, and the hostname routes to `api:8001`. The live `honeypot_updates` count remained 0 immediately after repair; a real Telegram update is still needed to verify row insertion end to end. Do not print or log tunnel credentials.

## Recurrence Guard

Expose tunnel connection state alongside honeypot receiver configuration, and alert when honeypot mode is enabled while the public callback is unreachable.
