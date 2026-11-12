# Diagnosis: sender rotation always resets to the bot

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Evidence and Root Cause

Live sender cache contains five eligible sessions, but delivery logs still select the bot. `BroadcasterService._rotate_sender` calls nonexistent `redis_srv.get` and `redis_srv.setex`; the broad exception handler resets the index to zero, which is the bot's position. `_send_via_anonymous_session` repeats the same invalid wrapper calls for its session index. The actual synchronous client is `redis_srv.client`.

## Fix and Verification

Use `redis_srv.client.get/setex` in both counters, run broadcaster tests, and observe a session delivery after cache refresh.
