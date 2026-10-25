# Outpost Operations

Outposts let remote runners work the shared validation queue from additional
IP addresses. A runner leases a small batch of pending tokens, checks each token
against Telegram from its own network, and posts the results back. No database,
Redis, or Celery credential ever leaves the main host.

The feature ships **disabled**. `OUTPOST_ENABLED` defaults to `False` in
`app/core/config.py`, and it must stay `False` until the remote host is
provisioned and the Oracle Always Free guardrails in
[oracle-always-free-runbook.md](oracle-always-free-runbook.md) are in place.

## Activation checklist

1. Confirm the gate is still off before you change anything:

   ```bash
   docker compose exec api python -c \
     "from app.core.config import settings, outposts_enabled; print(outposts_enabled(), settings.OUTPOST_LEASE_SIZE, settings.OUTPOST_LEASE_SECONDS, settings.OUTPOST_MAX_ACTIVE)"
   ```

   Expected: `False 25 300 3`.

2. Provision the remote host. Use the Oracle Always Free shapes and the
   dedicated compartment described in the runbook. Do not attach NAT gateways,
   extra VNICs, or paid load balancers.

3. Open one path only: HTTPS from the outpost to the API. In Compose the API
   publishes on `127.0.0.1:${API_PORT:-8011}` and Redis publishes on
   `127.0.0.1:${REDIS_PORT:-6379}`. A remote host therefore reaches the API
   through the existing Cloudflare Tunnel or a reverse proxy. Never expose
   Redis, and never copy `REDIS_URL`, `SUPABASE_SERVICE_ROLE_KEY`, or the
   `ENCRYPTION_KEY` to the outpost.

4. Set the outpost settings in the main host `.env`:

   ```dotenv
   OUTPOST_ENABLED=True
   OUTPOST_LEASE_SIZE=25
   OUTPOST_LEASE_SECONDS=300
   OUTPOST_MAX_ACTIVE=3
   MONITOR_API_KEY=<long-random-value>
   ```

   | Setting | Default | Meaning |
   |---|---|---|
   | `OUTPOST_ENABLED` | `False` | Runtime gate. `False` makes the lease endpoint return 403. |
   | `OUTPOST_LEASE_SIZE` | `25` | Maximum candidates returned by one lease. |
   | `OUTPOST_LEASE_SECONDS` | `300` | Lease lifetime before the dispatcher may reclaim the rows. |
   | `OUTPOST_MAX_ACTIVE` | `3` | Maximum concurrent outposts allowed to hold leases. |
   | `MONITOR_API_KEY` | required | Shared secret sent as `X-Monitor-Key`. |

5. Restart only the API so the new gate is loaded:

   ```bash
   docker compose up -d --no-deps api
   ```

6. Install the runner on the outpost. It needs Python 3.11 or newer and
   `httpx`. Copy `scripts/ops/outpost_runner.py`; do not copy the repository,
   `.env`, or any credential other than `MONITOR_API_KEY`.

7. Check the runner configuration without spending a lease:

   ```bash
   MONITOR_API_KEY=<key> python scripts/ops/outpost_runner.py \
     --api-base https://<main-host> \
     --dry-run
   ```

   `--dry-run` prints the effective endpoints and modes and makes no lease,
   Telegram, or result call. Use `--monitor-key-env OTHER_VAR` if the key lives
   under a different variable name; the key itself is never accepted as a
   command-line value.

8. Smoke test one lease cycle:

   ```bash
   MONITOR_API_KEY=<key> python scripts/ops/outpost_runner.py \
     --api-base https://<main-host> \
     --once
   ```

   `--once` leases, validates, posts results, and exits. Drop `--once` for the
   long-running loop, which polls every 30 seconds by default, and supervise it
   with systemd or tmux.

9. Confirm the round trip. Check that the validation queue drains, and from
   the outpost prove that Redis is not reachable:

   ```bash
   # Run on the outpost. Expect a failure or timeout.
   nc -z -w 3 <main-host> 6379 \
     && echo 'UNEXPECTED: Redis is reachable' \
     || echo 'OK: Redis is not reachable'
   ```

The API contract used by the runner:

| Endpoint | Auth | Body / response |
|---|---|---|
| `POST /monitor/outposts/lease` | `X-Monitor-Key` | Returns `{lease_id, expires_at, candidates: [{queue_row_id, token, source, meta}]}`. Returns 403 while `OUTPOST_ENABLED=False` and 429 when `OUTPOST_MAX_ACTIVE` live leases already exist. |
| `POST /monitor/outposts/result` | `X-Monitor-Key` | Accepts `{lease_id, queue_row_id, ok, http_status, reason}`. Returns 200 `completed`, 200 `duplicate` for an idempotent replay, or 409 for an unknown, mismatched, or expired lease. |

## Security model

- **The monitor key is a full admin secret.** `require_monitor_key()` compares
  the `X-Monitor-Key` header in constant time and gates every `/monitor`
  route, not just the two outpost routes. Treat the outpost host as trusted.
  Pass the key through the environment (the runner reads `MONITOR_API_KEY`
  by default, or the name given with `--monitor-key-env`), never as a
  command-line argument, because process arguments are visible in `ps`.
- **Leases bound the damage.** A runner can only see the candidates in its
  current lease. `OUTPOST_LEASE_SECONDS` caps how long those rows stay hidden
  from the dispatcher, and `OUTPOST_LEASE_SIZE` caps how many token values a
  single response can contain. Expired leases are reclaimed and late results
  are refused.
- **Redis stays on the main host.** Compose publishes Redis only on
  `127.0.0.1:${REDIS_PORT:-6379}`; containers reach it over the internal
  `theprawnhunter_net` network, which has no route from the outpost. The
  outpost has no `REDIS_URL`. Lease and result calls are the only channel.
- **Token values are decrypted per lease.** Encrypted credential rows are
  decrypted for the leased candidates only. A dry run prints configuration
  only and makes no external calls. Do not log a full lease response; it
  contains token values.
- **No anonymous access.** Both endpoints inherit the router-level
  `require_monitor_key` dependency. There is no unauthenticated variant.
- **Use HTTPS.** The key is a bearer secret. Plain HTTP on a public interface
  leaks it. Keep the tunnel or proxy in front of the API.

## Rollback

Disable the feature first. The code path stays, but leasing stops:

```dotenv
OUTPOST_ENABLED=False
```

```bash
docker compose up -d --no-deps api
```

Then stop the runner process on the remote host. In-flight leases expire after
`OUTPOST_LEASE_SECONDS` (default 300) and the dispatcher returns those rows to
the local queue. Verify the gate from outside:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  -H "X-Monitor-Key: $MONITOR_API_KEY" \
  https://<main-host>/monitor/outposts/lease
```

Expected: `403`. No schema migration or data repair is involved. If the key
leaked, rotate `MONITOR_API_KEY`, restart the API, update the runner
environment, and restart the runner.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `403` with a "disabled" detail on lease | `OUTPOST_ENABLED=False` | Set it to `True` in `.env` and restart the API. |
| `403` "Invalid or missing monitor API key" | Header absent or stale key | Export the current `MONITOR_API_KEY` on the runner and restart it. |
| `503` "Monitor API key not configured" | `MONITOR_API_KEY` empty on the server | Set it in `.env`, restart the API, and use the same value on the runner. |
| `429` "outpost lease cap reached" | `OUTPOST_MAX_ACTIVE` live leases already exist | Wait for a lease to expire or report its results. Raise the cap deliberately; `0` means no cap. |
| `409` unknown, mismatched, or expired lease on result | The lease aged out or the runner posted a lease id from a different batch | Raise `OUTPOST_LEASE_SECONDS` or lower `OUTPOST_LEASE_SIZE`; post the `lease_id` from the same lease response. |
| Runner exits 2 before leasing | `MONITOR_API_KEY` is missing from its environment | Export the key or point `--monitor-key-env` at the variable that holds it. |
| Lease returns `candidates: []` | No pending rows, rate budget exhausted, or the system is paused (`system:paused`) | Check the validation queue and the getMe budget before assuming the outpost is broken. |
| Lease succeeds but results are rejected | Lease expired or the runner clock is skewed | Raise `OUTPOST_LEASE_SECONDS` or lower `OUTPOST_LEASE_SIZE`; enable NTP on the outpost. |
| Every token reports a network error | Outpost egress to `api.telegram.org` is blocked | Allow outbound HTTPS 443; check the Oracle security list and any host firewall. |
| Work is validated twice | More than one runner is active, or a lease expired mid-batch | Keep `OUTPOST_MAX_ACTIVE` at 1 or 2 and size leases so a batch finishes well inside the lease window. |
| Queue grows while the outpost is idle | Runner exited after `--once`, or the API is unreachable | Run without `--once` under a supervisor and check runner logs for tunnel or TLS errors. |
| Remote host cannot reach the API | API is only bound to `127.0.0.1` | Route through the Cloudflare Tunnel or a reverse proxy; do not publish the API or Redis directly. |

Useful checks while debugging:

```bash
# Effective settings inside the API container
docker compose exec api python -c \
  "from app.core.config import settings, outposts_enabled; print(outposts_enabled(), settings.OUTPOST_LEASE_SIZE, settings.OUTPOST_LEASE_SECONDS, settings.OUTPOST_MAX_ACTIVE)"

# Prove Redis is not publicly bound
ss -ltn | grep 6379

# Lease with an explicit key (read the response shape only)
curl -sS -X POST -H "X-Monitor-Key: $MONITOR_API_KEY" \
  https://<main-host>/monitor/outposts/lease | head -c 400
```
