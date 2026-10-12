# Diagnosis: Honeypot pipeline — self-takeover churn + Cloudflare path block

Date: 2026-10-12 | Repo: `/home/bryan/Projects/theprawnhunter` (prawn-3410)

## Symptoms (Bryan's words)
"we are still taking over our own honeypot url winniethepooh and websocket to
new honeypot urls not working we dont have any messages"

## Root cause 1 (CONFIRMED, FIXED): self-takeover churn
`app/services/_scraper/strategies.py::WebhookStateService.prepare_polling` had
no guard for "webhook is already ours". Every scrape cycle for a honeypotted
credential called deleteWebhook on OUR OWN honeypot URL and re-registered it.
Log evidence (2026-10-12):
- 09:32:03 "TAKEOVER — deleted third-party webhook (https://winnethepooh.hong-yi.me/honeypot/receive/527bc0a1-...)"
- 10:08:51 "TAKEOVER — deleted third-party webhook (https://winnethepooh.hong-yi.me/honeypot/receive/5f5a5372-...)"
Each delete/re-register cycle drops in-flight Telegram deliveries.

Fix: early-return guard after getWebhookInfo — if
`HONEYPOT_WEBHOOK_URL` is a substring of the registered webhook URL, skip the
takeover path entirely (`honeypot_active: True` in evidence). Worker image
rebuilt, container restarted healthy 2026-10-12 ~20:05 SGT. Zero self-takeovers
observed since. Backup: /tmp/strategies.py.bak.20261012. Change is uncommitted
in the repo working tree (image has it baked).

## Root cause 2 (CONFIRMED, NEEDS BRYAN): Cloudflare blocks /honeypot/*
Every path on https://winnethepooh.hong-yi.me EXCEPT /health/ returns a
Cloudflare 403 "Attention Required!" block page — verified with and without
browser UA, for GET and POST. /health/ returns 200. This is a zone-level WAF
rule or tunnel ingress restriction; Telegram's webhook POSTs can never reach
the receiver, which is why honeypot_updates has 0 rows.

Fix required in Cloudflare dashboard (no API token available on 3410/VM):
allow traffic to winnethepooh.hong-yi.me/honeypot/* (WAF custom rules or the
tunnel's public-hostname ingress rules for the prawnhunter tunnel).

## Verified working
- Tunnel healthy; public /health/ → 200, local API /health/ → 200.
- Receiver end-to-end: synthetic Telegram update POSTed to local receiver with
  real secret + active credential_id → row landed in honeypot_updates → test
  row deleted. (Public-path test blocked by the Cloudflare 403 above.)
- HONEYPOT_MODE=True, URL=https://winnethepooh.hong-yi.me/honeypot, secret set,
  ALLOWLIST=AUTO.
- No Telegram session auth errors in 24h of worker logs; setWebhook calls to
  Telegram succeed.
- Legitimate third-party takeovers still work (counter-takeover detection
  intact); the arms race (C2 re-registering within minutes) is inherent.

## Note on "websocket"
There is no websocket in this codebase — the delivery path is Telegram webhook
HTTPS POST. "websocket ... not working" maps to the Cloudflare-blocked POSTs.
