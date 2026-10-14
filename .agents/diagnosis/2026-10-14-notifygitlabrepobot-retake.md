# Diagnosis: @notifyGitlabRepobot webhook re-take

Date: 2026-10-14 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

- Expected: An approved bot with a foreign webhook is pointed back at the configured honeypot receiver during scraping.
- Observed: The earlier report said no takeover for `@notifyGitlabRepobot` had appeared in logs. The current worker logs now show a successful takeover for that credential.
- Scope: One active credential, `e34b2ee0-ddcc-4479-bd9f-491a051d1ee5`.
- Onset: The report followed fix commit `867f25d`; its exact start time was not available.
- Repro: No forced scrape was run. Existing live worker logs and the credential's persisted scrape metadata were inspected.

## Evidence

1. The `flow.exfiltrate_chat` caller passes `credential_id=cred_id` into `ScraperService.scrape_history` (`app/workers/tasks/flows/scrape.py:393`). The scraper performs its Bot API webhook preflight after the Telethon stage returns at most ten messages (`app/services/scraper_srv.py:221-228`).
2. The running worker's imported source contains `_takeover_webhook_before_early_exit`, its Telethon-success call site, and the `takeover_only` branch in `WebhookStateService.prepare_polling`.
3. Runtime configuration has honeypot mode, receiver URL, and secret configured; the allowlist is `AUTO`, and the running `WebhookStateService.is_credential_allowed` returns true for this credential.
4. `docker logs --since 48h theprawnhunter_worker` records this credential's scrape beginning at 2026-10-14 00:52:40 UTC. At 00:52:51 UTC it logs `foreign webhook detected`, then `setWebhook succeeded ... (foreign webhook re-taken over)`, followed by `getUpdates unavailable while webhook is active ... continuing scraper flow`.
5. At 00:52:52 UTC the worker continued to the scrape kickstart and logged `@notifyGitlabRepobot`. The scrape audit recorded `bot_api_updates` as successful with `getUpdates_status=409` and `honeypot_active=true`, then classified the scrape as `no_new_messages`.
6. The live DB row for the credential is `active`; `last_scrape_at` is `2026-10-14T00:52:52.403746+00:00`, `last_scrape_reason` is `no_new_messages`, and its `bot_api_updates` attempt has `honeypot_active=true`.
7. The raw foreign-webhook log URL includes a Telegram Bot API token. Its value is intentionally omitted from this diagnosis.

## Root Cause

There is no remaining re-take failure for this credential in the inspected live run. The webhook check was reached and the foreign webhook was successfully replaced; the earlier absence of a log entry was superseded by this scrape cycle.

## Why It Was Not Obvious

The re-take occurs during a live scrape only when the Bot API reports a foreign webhook. Before this scrape, the available report did not include a target-specific runtime event. The takeover is recorded as a `setWebhook succeeded` message and a `bot_api_updates` attempt rather than a separate takeover task.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Change scraper code | None required for the observed run | Could disturb working takeover behavior | Do not change code |
| Continue to watch for a future re-take | Read-only log/DB observation | None | Only if a later live event is needed |

## Verification Plan

The target-specific success log, continued scrape log, persisted `honeypot_active=true` attempt, and live worker source together verify this item. No test or new scrape was needed.

## Recurrence Guard

Keep the existing scrape strategy audit fields and `setWebhook succeeded` log. Separately, redact Bot API tokens from foreign-webhook URL logs; no logging or credential changes were made in this item.
