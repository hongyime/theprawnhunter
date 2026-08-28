# Diagnosis: monitor stats timeout

Date: 2026-08-28 | Repo: X:\01 REPOSITORIES\theprawnhunter | Status: SUSPECTED

## Symptom Contract

Expected: `GET /monitor/stats` should return four aggregate counters quickly enough for operator monitoring.

Observed: another Codex session reported `/monitor/stats` is timing out. Other endpoints are reported healthy.

Scope: isolated to `/monitor/stats` based on the report.

Onset: unknown.

Repro: not reproduced locally; this diagnosis is based on repository evidence and the prior session's symptom report.

## Evidence

1. `app/api/routers/monitor.py:30-40` runs four Supabase/PostgREST exact count queries, including two against `exfiltrated_messages`.
2. `app/api/routers/monitor.py:36` counts all rows in `exfiltrated_messages`.
3. `app/api/routers/monitor.py:39` counts rows where `is_broadcasted = true`.
4. `database/init.sql:88-90` defines a partial `idx_messages_is_broadcasted` index only for `is_broadcasted = false`.
5. `database/init.sql:92-97` defines broadcast retry indexes focused on unbroadcasted rows, not total message cardinality or broadcasted rows.
6. `supabase/migrations/20260803000010_message_fts.sql` notes message search at 283k+ messages, confirming this table is already large enough for count paths to matter.
7. `tests/test_api.py:16-21` only checks error handling for stats, not successful count behavior or query shape.

## Root Cause

`/monitor/stats` computes dashboard counters synchronously using exact table counts on a growing append-heavy table. The total message count requires counting the full `exfiltrated_messages` table. The broadcasted message count filters on `is_broadcasted = true`, while existing partial indexes mainly optimize `is_broadcasted = false` operational queues. The endpoint also calls `.select("*", count="exact")`, which is a poor query shape for counts because it can request wide row data if the client fallback path is used.

## Why It Was Not Obvious

The endpoint is functionally correct on small datasets. Existing indexes help operational workflows around unbroadcasted messages, so the schema can look indexed while still leaving dashboard aggregate counts exposed to table growth.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---:|---|
| Query-shape patch | Change exact count calls to select a narrow column or head-only count, and add focused index for `is_broadcasted = true`. | Low | Good immediate mitigation, not enough forever for total count. |
| Cached stats endpoint | Cache `/monitor/stats` in Redis/app memory for a short TTL; serve stale-on-error. | Low-medium | Recommended near-term app fix. |
| Database stats table | Maintain `monitor_stats` counters by trigger, scheduled refresh, or write-path increments. Endpoint reads one row. | Medium | Recommended durable fix. |
| Materialized view | Refresh aggregate view periodically or on demand. | Medium | Good if exactness within refresh lag is acceptable and triggers are undesirable. |
| Approximate count | Use Postgres planner estimates for total rows. | Medium | Only acceptable if approximate dashboard numbers are explicitly okay. |

## Verification Plan

1. Add tests proving `/monitor/stats` returns the same response shape from the new path.
2. Add tests proving count queries do not fetch `*` row payloads.
3. In staging/production, run `EXPLAIN (ANALYZE, BUFFERS)` for old and new count strategy.
4. Load-test `/monitor/stats` with a realistic message table size and verify p95 latency stays below the monitor timeout.
5. Confirm failure behavior returns generic `500` or cached stale stats without leaking database errors.

## Recurrence Guard

Add a rule/test that monitoring endpoints must not run unbounded exact aggregate scans over append-only production tables. If exact counters are needed, they should come from a maintained stats table, materialized view, or bounded-cache layer.
