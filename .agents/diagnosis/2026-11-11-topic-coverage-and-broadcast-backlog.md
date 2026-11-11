# Diagnosis: Topic Coverage and Broadcast Backlog

Date: 2026-11-11 | Repo: /home/bryan/Projects/theprawnhunter | Status: CONFIRMED

## Symptom Contract

| Field | Finding |
|---|---|
| Expected | Active, monitorable topics should be rescraped within the configured interval; pending messages should use the configured broadcast batch capacity. |
| Observed | 7 active topics had captured messages in 24 hours. 24,011 messages were pending, oldest 17.8 days. |
| Scope | 1,002 current topic mappings and all pending `exfiltrated_messages` rows at the investigation snapshot. |
| Repro | Read active credential metadata and invoke the read-only pending-batch selector with its configured default behaviour. |

## Evidence

1. 1,002 mapped topics split into 408 scraped in the last 24h, 158 last scraped before that window, and 436 with no `chat_id`; 401 of the 408 freshly scraped topics produced no messages. Source: live `discovered_credentials.meta.last_scrape_at` / `chat_id` query at 2026-11-11 01:xx UTC.
2. The re-scrape query explicitly includes only `status = active` rows with non-null `chat_id`. Source: `app/workers/tasks/flows/scrape.py`, `_rescrape_active_logic`.
3. A valid token with no real accessible chat is deliberately retained as `active`. Source: `app/workers/tasks/flows/scrape.py`, `_enrich_logic` no-real-chats branch.
4. The 24-hour scrape audit sample contained 877 `no_new_messages`, 103 `user_agent_invite_failed`, 12 `webhook_conflict`, 5 success, 2 flood waits and 1 timeout across 447 unique credentials. Source: live `audit_logs` query (first 1,000 retained API rows).
5. The pending queue contained 24,011 rows: 24,003 immediately due and unclaimed, 6 retry-deferred, 2 freshly claimed; 24,000 had never been attempted. 20,722 were 7--30 days old; the oldest was created 2026-10-24 06:16:04 UTC. Source: live `exfiltrated_messages` pagination.
6. Pending rows belong to only 10 credentials / 6 topics. Five credentials share topic `964633` and contain 20,874 rows (86.9% of the backlog). Source: live joins to `discovered_credentials.meta.topic_id`.
7. Although `BROADCAST_BATCH_SIZE` is 200, the configured strict selector calls `oldest_pending_per_credential` and immediately returns one oldest row per credential. Source: `app/workers/tasks/flows/broadcast.py:261-297`.
8. A read-only invocation of that selector returned 5 rows (from 5 credentials, 3 topic ids), not 200. Source: live worker-container invocation at 2026-11-11 01:xx UTC.
9. The latest 60 minutes delivered 144 messages (2.40/min), with 12 retryable failures. Per-run batch logs were only 1--6 rows. Source: worker logs.
10. The sender pool has one monitor-bot identity; six `telegram_accounts` are active in the database, but the last 24-hour delivery logs contained 2,108 bot sends and zero anonymous-session sends. Source: live settings/DB query and worker logs.
11. Flood waits and timeouts are present. The separate "restricted sender" counter is not sender-specific: it counts scraper `bot_restricted:<chat_id>` cooldowns, which are written for target-chat access failures. Source: `app/services/scraper_srv.py:526-548, 623-627` and `app/workers/tasks/flows/broadcast.py:215-224`.

## Root Cause

### Topic coverage

This is mixed, not a single 997-topic outage. Among topics that were actually scraped in the last 24 hours, 401/408 produced no new messages, so most of the observed low activity is genuine lack of accessible new updates. However, 594/1,002 topic mappings were not scraped during that window: 436 have no discoverable `chat_id` and are intentionally excluded from rescraping, while 158 have a stale last-scrape timestamp. `active` currently means a valid credential, not an actively monitorable chat.

### Broadcast backlog

The primary bottleneck is strict per-credential ordering in the broadcast selector. It returns at most one oldest pending row per credential per run, despite the configured batch size of 200. Since the queue is concentrated in ten credentials and six topics, the effective batch is currently only five rows; retry backoff reduces it further. The minute schedule therefore caps practical drain rate near five rows/minute before Telegram failures.

One bot identity is doing all observed delivery. The six stored Telegram accounts are not contributing to broadcasts. Flood waits/timeouts then reduce the observed rate to 2.40 rows/minute. The "159 restricted senders" metric is misleading: it counts target-chat scrape restrictions, not available outbound senders; it lowers a nominal 200-row capacity that the strict selector never approaches.

## Why It Was Not Obvious

The dashboard-level batch configuration says 200 and there are six active account records, but the selector's strict RPC path bypasses the broader interleaving path and returns early. Topic counts also mix credentials with no accessible chat into the active-topic total.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Preserve ordering but fetch a bounded FIFO slice per ready topic/credential | Broadcast selector/RPC and test coverage | Must prevent overtaking retry-blocked rows | Primary fix |
| Separate active-with-chat from active-without-chat and expose scrape coverage | Credential state/monitor metrics | Migration/UI semantics | Primary observability fix |
| Verify/promote anonymous sender eligibility for stored sessions | Telegram group administration and sender diagnostics | Telegram flood limits still apply | Secondary capacity improvement |
| Count only actual outbound sender cooldowns in broadcast throttling | Broadcast health metric | Incorrect filtering could hide rate limits | Correctness fix, not primary throughput fix |

## Verification Plan

1. Confirm a broadcast run selects more than five rows while preserving per-topic order.
2. Confirm pending count falls at the intended sustainable rate and oldest pending age declines each hour.
3. Report separate totals for monitorable topics, freshly polled topics, stale topics, and no-chat credentials.
4. Confirm every sender shown as active in capacity metrics produces observed sends or is reported ineligible.

## Recurrence Guard

Add an invariant that a configured 200-row broadcast batch is not silently reduced to one row per backlog credential, and an alert when `last_scrape_at` exceeds the coverage SLO for a credential with a chat id.
