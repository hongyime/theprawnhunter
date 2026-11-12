# Diagnosis: anonymous-sender probe checks an unpopulated session list

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

| Field | Evidence |
| --- | --- |
| Expected | Broadcast rotation discovers active session files and uses every eligible anonymous account. |
| Observed | `eligible_anonymous_senders` returns zero and the broadcaster falls back to the single bot. |
| Scope | A fresh worker process before another user-agent operation calls `start()`. |
| Repro | Instantiate `UserAgentService` and call `eligible_anonymous_senders` before `start()`. |

## Evidence

1. Live aggregate inventory has six active database rows and six session files.
2. A read-only direct Telethon probe found five authorized anonymous monitor admins; one account is unauthorized.
3. In the fresh production worker, `len(user_agent.sessions)` is `0`, while the Redis sender cache is `[]` and `eligible_anonymous_senders` returns `0`.
4. `UserAgentSessionLifecycle.__init__` initializes `self.sessions = []`; `_discover_sessions()` populates it, but `eligible_anonymous_senders` loops over `self.sessions` without calling discovery.
5. Worker logs after the release show broadcasts still using the bot identity.

## Root Cause

The eligibility path is called before any lifecycle operation that discovers session files. It loops an empty in-memory list, caches the false empty result for five minutes, and prevents rotation from ever reaching the real account files.

## Fix Options

| Option | Changes | Risk | Recommendation |
| --- | --- | --- | --- |
| Discover sessions at the eligibility method's boundary | One shared method plus test | Repeated directory/DB reads per cache miss | Recommended |
| Depend on callers to call `start()` first | Every caller and ordering contract | Easy to regress | Reject |

## Verification Plan

1. Unit-test eligibility invokes discovery before evaluating session paths.
2. Clear the stale cache and confirm the live worker reports five eligible senders.
3. Observe post-release worker logs for anonymous-session delivery.

## Recurrence Guard

Every public method that consumes `self.sessions` must establish or document session discovery itself.
