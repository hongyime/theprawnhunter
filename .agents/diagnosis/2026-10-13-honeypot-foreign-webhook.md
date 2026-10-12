# Diagnosis: Approved honeypot bots are skipped on foreign webhooks

Date: 2026-10-13 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

- Expected: When an approved bot has a foreign Telegram webhook, restore the configured honeypot receiver and continue the scrape flow.
- Observed: The worker treats the foreign webhook as a conflict and exits before the honeypot registration code runs.
- Scope: The reported bot is stuck in this state; the code path applies to any credential that reaches this scraper check.
- Onset: Not provided.
- Repro: Return a foreign URL from `getWebhookInfo`, enable honeypot mode with the receiver and secret configured, and allowlist the credential.

## Evidence

1. `WebhookStateService.prepare_polling` returns `can_poll=False` with `WEBHOOK_CONFLICT` when `allow_delete` is false (`app/services/_scraper/strategies.py:197-207`).
2. The honeypot `setWebhook` request is only reached after successful `deleteWebhook` (`app/services/_scraper/strategies.py:236-247,269-339`).
3. Scraper construction uses `TELEGRAM_DELETE_WEBHOOK_FOR_SCRAPE`, whose default is `False` (`app/services/scraper_srv.py:92-94`, `app/core/config.py:82`).
4. The existing `setWebhook` request uses `/receive/{credential_id}`, `secret_token`, the five required update kinds, `drop_pending_updates=false`, and retries up to three times (`app/services/_scraper/strategies.py:293-326`).
5. The existing registration gate requires honeypot mode, configured receiver and secret, and an allowlisted credential or `AUTO`; an empty list is default-deny (`app/services/_scraper/strategies.py:277-291`, `app/core/config.py:116-125`).

## Root Cause

The default delete policy causes foreign webhooks to return as terminal conflicts before the existing allowlisted honeypot registration code can run. As a result, the bot keeps its foreign delivery address.

## Why It Was Not Obvious

The code already contains a working honeypot registration request, but it sits after a delete-policy gate. The default setting prevents the request from being reached.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Reuse the existing gated registration request before the delete-policy return, and share it with the current post-delete path | Small change in `strategies.py` plus regression tests | Approved credentials with full honeypot configuration can overwrite a foreign webhook, as intended | Recommended |
| Globally enable deletion of foreign webhooks | Changes global scraper behavior for every credential | Could alter other bots' webhook registrations | Reject |

## Verification Plan

Add unit coverage showing an approved foreign webhook is set to the configured honeypot receiver and polling proceeds, while a non-approved credential is left untouched. Run scraper and honeypot-related unit tests.

## Recurrence Guard

Yes. Keep the approved versus unapproved behavior in unit tests so foreign webhook recovery cannot bypass the honeypot gates.
