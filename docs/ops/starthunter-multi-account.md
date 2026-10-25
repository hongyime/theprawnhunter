# /starthunter Multi-Account Operations

`/starthunter` is the interactive Telethon login flow: an operator (or, when
enabled, any user) DMs a monitor bot, supplies a phone number and login code,
and the bot writes a `.session` file that the scraper, broadcaster, and media
archive workers use as user-agent accounts.

This runbook covers adding several accounts, tuning the per-user rate limit,
and the security constraints that apply when the flow is reachable by the
public.

## What the limit controls

Each Telegram user ID that runs `/starthunter` is counted in Redis. The
counter is keyed `counter:rl:starthunter:<user_id>` and expires after the
configured window. Once a user **exceeds** the allowance inside the window,
further `/starthunter` invocations are refused until the window expires.

| Setting | Default | Meaning |
|---|---|---|
| `STARTHUNTER_RATE_LIMIT_PER_DAY` | `3` | Attempts allowed per user per window. `0` disables the check entirely. |
| `STARTHUNTER_RATE_WINDOW_HOURS` | `24` | Counter lifetime in hours. The Redis TTL is `WINDOW_HOURS * 3600` seconds. |

Defaults reproduce the historical behaviour: 3 attempts per 24 hours.

The limit is per **Telegram user ID**, not per phone number or session. One
operator adding many accounts from the same Telegram user is subject to the
same allowance. Redis failures do not block logins: if the counter cannot be
read or incremented, the attempt is allowed and the failure is logged at
debug level.

## Adding multiple accounts

Repeat the flow once per account, from a Telegram user that is allowed to use
`/starthunter`:

1. DM the monitor bot and send `/starthunter`.
2. Send the phone number in E.164 form (`+1234567890`).
3. Send the login code as Telegram delivers it. Telegram rejects forwarded
   codes, so type the digits with spaces, dashes, or commas (`1 2 3 4 5`).
4. Enter the 2FA password if the account has two-step verification enabled.
5. Send `/starthunter` again for the next account when the previous flow has
   finished.

Each successful login produces `sessions/account_<digits>_<unix_ts>.session`
and upserts a `telegram_accounts` row with `status = active`. Re-logging the
same phone deletes the old session file and old DB rows for that phone before
saving the new one, which avoids MTProto session conflicts and FloodWait
cooldowns.

### Bulk onboarding

For a batch of accounts, temporarily raise or disable the gate:

```dotenv
# .env
STARTHUNTER_RATE_LIMIT_PER_DAY=0
```

Restart the bot service so the setting is picked up:

```bash
docker compose up -d --no-deps bot
```

Add the accounts, then restore a finite limit and restart again:

```dotenv
STARTHUNTER_RATE_LIMIT_PER_DAY=3
STARTHUNTER_RATE_WINDOW_HOURS=24
```

`0` is a supported value (not a validation error) and means "never refuse on
this counter". Negatives are rejected at startup.

### Changing the window

The Redis key namespace did not change when the window became configurable, so
no manual reset is required. The new TTL is written by `incr` on the next
`/starthunter` attempt; existing counters keep their previous expiry until
that attempt refreshes it. If you want the new window to apply immediately to
everyone, clear the counters:

```bash
docker compose exec redis redis-cli --scan --pattern 'counter:rl:starthunter:*' \
  | xargs -r docker compose exec -T redis redis-cli del
```

Inspect a single user's counter and remaining TTL:

```bash
docker compose exec redis redis-cli get counter:rl:starthunter:<user_id>
docker compose exec redis redis-cli ttl counter:rl:starthunter:<user_id>
```

## Security caveats

- **`ALLOW_PUBLIC_STARTHUNTER=True` is dangerous.** It turns every Telegram
  user into a potential session-pool contributor. A stranger can add their own
  account, and that account's session is then used by background scraping and
  broadcast jobs. Keep it `False` unless you are deliberately running an open
  onboarding bot, and pair it with a finite rate limit.
- With `ALLOW_PUBLIC_STARTHUNTER=False`, only numeric IDs in
  `WHITELISTED_BOT_IDS` (plus the anonymous-admin ID inside the monitor group)
  may start the flow. Usernames are rejected because they are reassignable.
- Session files are credentials. They are written with owner-only permissions
  and must never be committed, copied to shared storage, or attached to
  tickets. Removing an account means deleting its session file **and** its
  `telegram_accounts` row.
- Logging in many accounts from one IP, or re-logging the same account too
  often, can trigger Telegram FloodWait or account restrictions. Prefer a small
  pool of healthy accounts over frequent re-logins, and keep the per-user limit
  enabled in production.
- The login flow deletes the bot/user conversation and Telegram service
  notifications after a successful login. Do not disable that cleanup path;
  it is what prevents the phone number and code from lingering in the chat.

## Verifying the configuration

```bash
# Confirm the effective values inside the Bot container
docker compose exec bot python -c \
  "from app.core.config import settings; print(settings.STARTHUNTER_RATE_LIMIT_PER_DAY, settings.STARTHUNTER_RATE_WINDOW_HOURS)"

# Unit coverage for the gate (no live Redis required)
.venv/bin/python -m pytest tests/unit/test_starthunter_limit.py -q -p no:cacheprovider
```
