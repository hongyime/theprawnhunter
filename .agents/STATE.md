# theprawnhunter — STATE

## Current task - Step 3, Fix 4 only: preserve and expose URLs (2026-10-16)

- **Status:** [x] Implemented and live-verified Fix 4 only. Fix 1 commit 2e2fb62 remains intact. Fixes 2, 3, 5 and 6 were not implemented; no historical backfill, old-message replay, transcription or subagents. No extracted URL was fetched, resolved or visited. No credentials were printed.
- **Stored URL metadata:** app/services/honeypot_urls.py extracts plain HTTP(S)/www URLs and Telegram url/text_link entities from full text and captions before the existing content cut-off. file_meta.urls is a stable-order, exact-deduplicated URL list; file_meta.text_links preserves each distinct displayed-label/destination pair. Emoji-aware UTF-16 slicing handles Telegram entity positions. URLs without schemes are preserved when supplied by Telegram url entities; hidden destinations retain queries/fragments exactly. Messages without URLs retain their existing metadata. This adds searchable JSON metadata, not a new dashboard search endpoint, telemetry-table feed or physical SQL index; no schema migration was needed.
- **Broadcast:** The unchanged ID/sender header is followed by the normal content and a Links section containing up to three URLs, with an omitted count when needed. Long display URLs are shortened with a full-URLs-saved notice; complete URLs remain in metadata. The link-bearing caption stays within 1,024 UTF-16 units, reserving room for the Links section. Existing no-link rendering, Fix 1 descriptions and media transfers are unchanged. Telegram text-link previews are disabled for URL-bearing messages, and new URL/label metadata is excluded from the existing media debug log so destinations are not copied into those logs.
- **Regression/verification:** The initial real-extractor/broadcaster test failed with missing file_meta.urls for both text and caption inputs. Eleven new cases now pass, covering hidden/visible links, duplicates, labels, emoji offsets, caption photo/document metadata, URLs beyond the stored-text cut-off, long/many URLs, punctuation and malformed offsets. Only Telegram HTTP replies are replaced in the send tests. 127 focused tests pass; final full unit suite: 622 passed, the same two unrelated failures (non-admin telemetry wording and three raw-ID logs in unchanged group_archive.py). Ruff passes on the new helper/test and broadcaster; honeypot_srv.py retains its verified pre-existing local uppercase-variable lint finding. Standalone type checks pass on the helper/test and report no errors on newly added lines; 28 errors elsewhere in the existing service files remain. Editor LSP repeatedly timed out; no clean LSP result is claimed. No error suppressions were added.
- **Deployment:** Built/recreated ONLY API and worker using Compose --no-deps. A final typing correction made preview options explicit; both services were refreshed again with that exact source. Final API 19e28a11a682 started 09:50:40Z; final worker f940f0c740ee started 09:50:39Z. Both are healthy with zero restarts. All three changed production file hashes match checkout in both containers. Bot, beat, Redis and cloudflared retained their original IDs/start times; API reports active. No runtime configuration or queue was altered.
- **Live proof:** Two fresh, explicitly labelled controlled messages were submitted to the actual receiver with reserved .invalid URLs, no sender user ID and chat ID zero, so no user redirect could be sent. With 1,482 existing queued jobs at preflight, the real targeted worker function was invoked only for each newly captured fixture; normal already-sent guards protect the queued duplicate. No processing function, storage client or Telegram reply was faked. Both captures persisted the exact plain and hidden URLs and the hidden displayed label; a real database JSON-containment query matched the hidden destination. Telegram's real HTTP responses confirmed the Links section and disabled previews, and normal evidence JSON delivery completed. Initial proof: update 1792143964/message 1109243. Final exact-source proof: update 1792144270/message 1109316 at 2026-10-16T09:51:20Z. Both new fixture records are retained; no historical record was backfilled or rebroadcast.
- **Log watch:** Final window 2026-10-16T09:50:39Z-09:55:54Z (5m15s): 269 worker task successes, no tracebacks, critical logs, honeypot-send or evidence-send failures; API had no traceback, HTTP 5xx or persistence-failure matches. One unrelated older-media-not-found error remained in an unchanged media path. API/worker/bot/beat/Redis were healthy with zero restarts; tunnel remained running.
- **Shipment record:** These notes accompany the one requested fix commit, fix(honeypot): preserve and display message URLs. The completion handoff reports its exact hash and remote confirmation; only the Fix 4 helper/integration/tests and authorized notes are included. The pre-existing untracked handoffs directory is untouched. Historical backfill remains HELD for Bryan; no next fix is authorized.

## Previous task - Step 2, Fix 1 only: readable service-event broadcasts (2026-10-16)

- **Status:** [x] Implemented and deployed Fix 1 only. New empty-text honeypot broadcasts receive a display-only description from the saved raw update. Header format is unchanged. Text, captions and recognized media handling remain unchanged. Extractor/database payloads, schema, retention, webhook filters, transfer behavior and transcription were NOT changed. No historical backfill or old-message replay occurred; fixes 2-6 remain out of scope, historical backfill remains HELD for Bryan, and transcription is not in scope at all.
- **Implementation:** app/services/honeypot_rendering.py makes a separate display copy with plain descriptions for topic/member/pin/chat events and unsupported animation/location/contact/poll/etc. The existing worker reads the raw update only when a pending summary has empty content and media_type text. It passes the display copy to the unchanged broadcaster while all persistence/evidence code continues using the original record. Existing already-broadcast guards and the immediate pre-send ownership check remain in place. Neither receiver nor extractor was modified.
- **Regression:** Before the renderer was added, the real extractor and real broadcaster produced only the header and the test failed on the missing Forum topic created body. The finished 38-case file uses only a Telegram HTTP-response replacement, not fake extractors/renderers/broadcasters or database objects; it covers all four supported message/post/edit envelopes, topic/member/pin events, animation/location/contact/poll/unknown content, non-mutation, and preserving existing text/captions/media. The test setup first needed a request-cleanup correction; the later red result specifically confirmed the missing description.
- **Local verification:** 76 focused honeypot tests passed; full unit suite: 611 passed, two known unrelated failures remain (non-admin telemetry reply wording and three raw-ID log findings in unchanged group_archive.py). Ruff passed on the new renderer/test; worker syntax lint and diff whitespace checks passed; Compose config passed. Standalone basedpyright reports zero errors for the new renderer/test and none in the modified worker send block (36 errors elsewhere in that existing worker module). The editor LSP repeatedly timed out, so there is no clean LSP result to claim. No type suppression was added.
- **Deployment:** Built only the worker image, then ran docker compose up -d --no-build --no-deps worker. New worker b87c1729a86f started 2026-10-16T09:25:31Z on image config sha256:3e1ceef6bc11b3b2f762e5115d5ac7ffe0c988176f1948f265ad5da05d49f2fd; it is healthy with restart count zero. SHA-256 of both changed production files matches the deployed worker. API, bot, beat, Redis and cloudflared retained their prior container IDs/start times; API reports active.
- **Live verification:** At 2026-10-16T09:28:10Z a fresh, explicitly labelled Fix1LiveCheck fixture went through the deployed real extractor, renderer and broadcaster to the existing monitor topic. Telegram's actual HTTP response confirmed the unchanged header plus Forum topic created and message_id 1108944. This was a controlled live rendering/send check, not an organic webhook event or a full receiver/queue test. No database row was inserted, no historical row was updated/rebroadcast, and no credentials were printed. The first attempt stopped before sending because the check incorrectly assumed the configured monitor group was numeric; the check was corrected to support the existing username configuration without changing application code/config.
- **Log watch:** 2026-10-16T09:25:31Z to 09:31:18Z (5m47s): 90 tasks succeeded, zero tracebacks/critical/honeypot-send/evidence-send errors; all health-checked services remained healthy with zero restarts. Not a wholly clean log: 28 Telegram chat-bot-limit errors and two older-media-not-found errors came from unchanged invite/media paths. They were not folded into this fix.
- **Shipment record:** These notes accompany the one requested fix commit, fix(worker): describe empty honeypot events, together with its rendering code/test and the previously uncommitted Step 1 diagnosis. The exact self-referential commit hash and remote confirmation are reported in the completion handoff. No other files are included; the pre-existing untracked handoffs directory is left alone. No follow-on fix is authorized by this step.

## Previous task - Honeypot content diagnosis (2026-10-16)

- **Status:** [x] Step 1 diagnosis complete on main. Read-only source/runtime/database inspection; only STATE.md and JOURNAL.md writes authorized. No code changes, commits, restarts, task dispatches, Telegram sends, or credential output. Fixes await Bryan's approval.
- **Verification:** The eight inspected parser/router/service/worker/archive files match the running worker by SHA-256; the router, honeypot service, and shared media parser also match the running API. Both forwarding and topic broadcasting are enabled; AUTO_ARCHIVE_MEDIA is enabled. Nine synthetic message variants were passed only through the real, side-effect-free extractor in the API container with bytecode writes disabled. Live DB inspection printed field names, lengths, and status flags only.
- **Actual path:** app/api/routers/honeypot.py:152 saves the complete incoming JSON to honeypot_updates, then awaits app/services/honeypot_srv.py:168 to insert a shortened exfiltrated_messages record and queue flow.broadcast_honeypot_message. The worker does NOT initially store the captured message; it fetches it, broadcasts it, and records delivery. app/utils/parsing.py is an older compatibility helper, not this ingest path (repository search finds only its facade and tests).
- **Common kept fields:** Credential, negative update ID, sender name (128 characters), sender user ID, text/caption (2,000 characters), media type, source/update/original-message/chat IDs, receipt timestamp, and delivery state. Telegram source date and extra details remain in raw JSON. Broadcaster summary including the ID/sender header is cut to 1,024 characters for both text and media; the database copy is not changed by that cut (honeypot_srv.py:68-106; broadcaster_srv.py:302-315).
- **Per-type findings:** Text/captions are captured with those limits. Photos keep the last photo-size entry's file ID, unique ID, size, width and height. Documents/audio/video keep available file ID, unique ID, filename, MIME and size. These four types are downloaded using the source bot and re-uploaded at broadcast time, not stored as binary database attachments. Voice/video notes/stickers retain that same generic file metadata but are outside ARCHIVE_MEDIA_TYPES and only produce a media note, not a transferred file. No transcription exists in this path. Durations, video dimensions, sticker emoji and other extra fields remain raw-only (app/domain/media.py:12-40; honeypot_srv.py:23-28; broadcaster_srv.py:21,230-286,346-435).
- **Links/service events:** Visible URLs stay inside text; the honeypot path does not call the separate scraper telemetry indexer and does not copy entities/caption_entities to its normalized metadata. Hidden-link destinations survive only in raw JSON/evidence JSON, not the plain-text summary. Service messages inside message/post/edit updates become empty text records; event details remain raw-only. Other unsupported content such as animation-only/location/contact/poll messages can also become header-only broadcasts. Callback and inline queries have explicit content prefixes and type notes, so do not produce that exact bare-header shape (honeypot_srv.py:68-165; flows/scrape.py:80-143).
- **Exact live case confirmed:** Stored telegram_msg_id -967576045 has empty content, media_type text, is_broadcasted true, a real broadcast message ID, and no evidence-file status. Its matching raw update exists and is a message with forum_topic_created, no text/caption/photo/document. The source was a topic-creation notice, not a lost text/photo/file. Normalization discarded the event description from the broadcast, not from raw storage. Do not copy sender identity or raw payload into state.
- **Evidence attachment:** Current worker sends honeypot-evidence-<update_id>.json after the summary, containing untruncated text and the raw update; it retries pending evidence separately. This is a JSON description, not the source media. Already-broadcast rows without a pending evidence flag, including the exact live example, are skipped rather than backfilled (flows/honeypot.py:44-48,155-246,267-356,644-669).
- **Archive diagnosis:** Not wholly dead: available worker logs from a --since 24h read (current container uptime about seven hours) show 126 successful photo uploads and 212 video uploads. They show 1,714 auto-archive skips: 1,073 session_unavailable and 641 not_found; no successful user-session archive lines in this window. Archived Attachment calls are reachable, but the honeypot fallback passes negative update ID instead of file_meta.message_id and resolves a bot username/credential chat instead of file_meta.chat_id. /getfile rejects negative numeric IDs; UUID lookup still passes the wrong negative ID and ignores nested source-chat metadata. The transfer helper itself downloads to a temporary file, uploads, and deletes it; it needs a usable user session with source access (broadcaster_srv.py:156-213,400-426; commands.py:206-298; group_archive.py:373-519). _schedule_auto_archive has no callers, but the awaited fallback is active.
- **Upstream/retention gaps:** Webhook registration requests message, callback_query, edited_message, channel_post and inline_query, but omits edited_channel_post despite extractor support; other update families are raw-only if delivered and otherwise excluded upstream. Raw JSON is not permanent: live cleanup is enabled with 30-day redirected/completed and 60-day unredirected retention (strategies.py:114-121; retention_tasks.py:94-105).
- **Proposed fixes, NOT implemented:** Label service/unsupported content explicitly; transfer voice/video-note/sticker files; repair archive source IDs/chat selection and /getfile negative-ID lookup; preserve/index URL entities; avoid silent summary/text cuts while retaining full evidence; include intended update families in webhook registration; make delivery/archive failures clear and test all types. Decide historical evidence backfill and retention policy explicitly. No production archive attempt was triggered by this diagnosis.

## Previous task — Production redirect evidence (resumed 2026-10-16 SGT)

- **Job:** Complete three ordered production fixes on machine 3410: persist and log Telegram redirect delivery evidence, investigate `fenced_out` without changing fence logic, then retain container logs for at least seven days. Finish, live-verify, commit, and push each fix before starting the next.
- **Fix 1 status:** [x] Deployed and healthy; Bryan explicitly waived fresh live DB/log correlation after the configured canary proved revoked and no active credential matched an owned monitor bot. A typed Telegram delivery result preserves real `sendMessage` IDs, callback/inline acknowledgements retain a null ID with their method, and the initial/touch-2/touch-3/proactive rows persist stage-specific evidence only after fenced writes succeed. Success logs include credential, pseudonymous subject, method, message ID, and timestamp.
- **Fix 1 contract:** Store real `message_id` values returned by Telegram `sendMessage` on the matching `honeypot_updates` row and emit privacy-safe success evidence. `answerCallbackQuery` and `answerInlineQuery` return only `true`, so their method/acknowledgement must be recorded with a null message ID; never invent an ID.
- **Local verification:** 33 focused redirect tests passed; the full unit suite reached 573 passed with exactly two pre-existing failures (`test_telemetry_command_restricts_non_admin` text mismatch and three unrelated `group_archive.py` raw-ID log findings). Ruff passed for directly changed modules/tests; basedpyright/py_compile passed; Compose config validated; the worker image built successfully as `theprawnhunter-worker:latest`. The Python LSP daemon repeatedly timed out, so no LSP result is available.
- **Production migration:** Applied `20261016000001_honeypot_redirect_delivery_evidence.sql` through the Supabase Management API (HTTP 201), reapplied it successfully to prove idempotency, and independently verified nullable `redirect_method` (`text`) plus four stage message-ID columns (`bigint`). No data backfill occurred.
- **Worker rollout:** Recreated only `theprawnhunter_worker` from image `sha256:d08a2d82760a2e946ff69e455e2193a6a83658be02e19e06a162184d5220104f`. It reached healthy with restart count 0; API, bot, beat, Redis, and tunnel retained their previous container identities/uptimes.
- **Live watch:** Worker, API, bot, beat, and Redis remained healthy; API returned `{"status":"active"}`. Over 10 minutes the worker had zero restarts and zero matches for traceback, critical, fence, delivery-failure, or evidence-persistence errors. No post-deploy redirect occurred, and all five new evidence columns remain empty.
- **Canary check:** Bryan authorized a controlled canary. The configured `CANARY_CREDENTIAL_ID` resolves to a revoked credential; live Telegram `getMe` and monitor-chat membership checks both returned unsuccessful. No message was sent, no synthetic row was inserted, and no token was printed or persisted.
- **Waiver:** Bryan explicitly accepted migration, tests, worker-only rollout, service health, and the clean 10-minute watch in place of a fresh live message-ID correlation. No arbitrary historical user was messaged.
- **Next gate:** Fix 1 is shipped. Fix 2 was completed as a read-only investigation with no fence change; proceed to the seven-day container log-retention fix.

### Fix 1 shipment — 2026-10-16

- Commit `1ac0846` (`fix(worker): persist redirect delivery evidence`) was pushed to `origin/main`; local and remote HEAD both resolve to `1ac0846b8de5cdd094de768400d801d4375e4160`. The worker remains healthy on the verified image with restart count 0.
- **Fix 2 status:** [x] Investigation complete; no fence logic changed. The persistent Redis `celery` queue held three generations of `flow.honeypot_redirect_one` jobs for the same four rows while draining from 1,922 to 432 entries. The first eight jobs carried superseded claim tokens and were correctly rejected immediately pre-send by `_is_fence_valid()`; surrounding sweeps reported `dispatched: 0`, proving those jobs came from the existing queue rather than a new dispatch. The final four jobs matched the latest claims, observed that credential `1f82cee6-...` was already `revoked`, attempted no Telegram send, and terminalized all four rows with `redirect_attempt = 3`, cleared claims, `redirected_bot = token_revoked`, and a non-null completion timestamp. Across the post-deploy log there were exactly 12 redirect-one receipts: eight fenced and four revoked skips.
- **Fix 2 conclusion:** The observed `fenced_out` results were expected stale-work rejection during backlog drainage, not a regression in fence logic. Worker health remained good and no source, production data, credentials, or runtime configuration was manually changed during the read-only investigation.
- **Fix 2 shipment:** Commit `1acb191` (`docs(agents): record redirect fence investigation`) was pushed to `origin/main`; local and remote HEAD both resolve to `1acb1912a17fed56a375852025120c73639b866d`.
- **Fix 3 status:** [x] Deployed and live-verified. All live services previously used `json-file` rotation capped at 30 MB (`10m` x 3). The busiest service produced about 5.3 MB over two hours while draining the abnormal queue backlog, or roughly 64 MB/day. Raising the shared rotation count to 100 provides a bounded 1 GB per-container budget, more than 15 days at that measured peak, and at most 6 GB for the six live containers against 242 GB free host storage.
- **Fix 3 local verification:** Base, full-plus-honeypot, and production-overlay Compose renders all passed `docker compose config --quiet`; the rendered services all inherit `max-size: 10m` and `max-file: 100`. YAML LSP is not installed, so no LSP diagnostic result is available.
- **Fix 3 rollout:** Recreated exactly the six running services with `--no-build`; every container now reports `json-file` options `max-size: 10m`, `max-file: 100`. Redis, API, worker, beat, and bot are healthy with restart count 0; cloudflared is running without a configured health check. The bot was briefly unhealthy while honoring the previous instance's 120-second Redis poll-lock TTL, then acquired the lock, resumed polling/watchdog operation, and became healthy without intervention. API returns `{"status":"active"}`, Redis returns `PONG`, and the Celery queue continues draining (407 entries at checkpoint). One pre-send fence from the remaining backlog rejected stale work before any outbound send; no fence logic changed.
- **Fix 3 live watch:** During the final 10-minute window, the queue drained from 404 to 361. All six containers remained running with the new log options; Redis, API, worker, beat, and bot remained healthy with restart count 0, cloudflared remained running, API returned active, and Redis returned PONG. There were zero new matches for traceback, critical, HTTP 5xx, unhandled errors, evidence-persistence failures, immediate pre-send fences, bot conflicts/errors, beat errors, tunnel errors, or Redis corruption warnings.
- **Fix 3 code commit:** `a3189d0` (`fix(compose): extend container log retention`) contains only the shared Compose rotation change.
- **Fix 3 shipment:** Deployment record commit `0a2e554` (`docs(agents): record log retention rollout`) and code commit `a3189d0` were pushed to `origin/main`; local and remote HEAD matched at `0a2e554e1e6cc32de9e572417cade63db517efc4`. A final live check found all health-checked services healthy with zero restarts, cloudflared running, API active, all six containers retaining `10m` x 100 options, and the queue down to 349.
- **Next gate:** None. All three ordered production fixes are implemented or investigated as requested, live-verified, committed, and pushed.

## Previous task — Fix 3 audit issues on 3410 (started 2026-10-14 21:10 SGT)

- **Job:** Fix the 3 honeypot issues discovered in the 2026-10-14 20:50 SGT audit. One fix at a time, live verification, commit + push, STATE.md and JOURNAL.md updated after every step.
- **Discipline (Bryan):** After EVERY small task, update STATE.md (tick the checkbox) AND append a JOURNAL.md line (what was done, why, commit hash). Commit STATE.md promptly with each fix commit.
- **Task list:**
  - [x] Fix 1. API container cannot reach database — configured explicit DNS in docker-compose.yml; rebuilt API container; verified live DNS resolution, real inserts succeeding, 500s dropped to 0, Telegram pending updates draining.
  - [x] Fix 2. Queue jammed + redirect jobs looping forever — inspected queue contents without purging; added Redis non-blocking lock & 15s debounce on sweeper; broke infinite retry loop by removing stale 10m fence cutoff; terminated terminal dead tokens (HTTP 401) and blocked users (HTTP 403) with permanent non-null redirected_at; verified concurrency 2 is sufficient; rebuilt worker; verified live queue draining (796 -> 510) and zero fenced_out churn.
  - [x] Fix 3. All 4 user sessions on cooldown — verified graceful degradation (saved topic IDs fallback working via bot API, media auto-archive skipped cleanly with retry scheduled, new-bot topic creation cleanly fails with retry), recorded exact session recovery times (earliest 2026-10-15T06:02:02.954943+00:00 UTC). Zero user sessions touched, polled, or poked.
- **Next:** All 3 audit issues on 3410 fixed and verified live.

### Fix 3 evidence — 2026-10-14
- Method: Strictly read-only verification of existing live worker logs and application code. Zero sessions touched, queried, or poked.
- Expected Recovery Times (from live worker logs):
  - Session 1: `account_6596647252_1791784103` -> cooldown until `2026-10-15T06:02:02.954943+00:00` (~16.2h from now)
  - Session 2: `account_6592348112_1791783641` -> cooldown until `2026-10-15T06:02:03.950009+00:00` (~16.2h from now)
  - Session 3: `account_60197282165_1791784179` -> cooldown until `2026-10-15T06:02:03.961395+00:00` (~16.2h from now)
  - Session 4: `account_6584731565_1791785791` -> cooldown until `2026-10-15T06:22:40.963392+00:00` (~16.5h from now)
  - Earliest recovery: `2026-10-15T06:02:02.954943+00:00` (14:02 SGT on 2026-10-15).
- Graceful Degradation Proofs:
  1. Broadcasts with saved topic IDs:
     - Broadcaster logs: `[Broadcaster] Could not verify saved topic ... trusting the saved topic ID` and sends via Bot API (`📤 [Broadcaster] Sending via Bot: 8737065943...`).
     - Live broadcast successes:
       - `[2026-10-14 13:40:32,322: INFO/ForkPoolWorker-2] ✅ [Broadcaster] Successfully sent video (3283987 bytes, filename=777777.mp4)`
       - `[2026-10-14 13:40:32,467: INFO/ForkPoolWorker-2] ✅ Broadcasted msg 10a6a7b1-ff4b-42f0-9e4e-934e844d8f07`
       - `[2026-10-14 13:41:04,707: INFO/ForkPoolWorker-1] ✅ [Broadcaster] Successfully sent photo (165654 bytes, filename=photo.jpg)`
       - `[2026-10-14 13:41:04,796: INFO/ForkPoolWorker-1] ✅ Broadcasted msg 59e990d6-c118-4563-b6b8-eea001ed9230`
  2. Media archiving failure handling:
     - For photo/media updates requiring Telethon user session download, worker skips archive gracefully and logs:
       - `[2026-10-14 13:47:34,299: WARNING/ForkPoolWorker-1] [Broadcaster] Auto-archive skipped for msg=124215: session_unavailable No usable user session.`
       - `[2026-10-14 13:47:34,526: WARNING/ForkPoolWorker-1] Audit: broadcast.failed | cred_id=1c4e81ba-... | reason: 'media_archive_session_unavailable', retryable: True, next_retry_at: '2026-10-15T10:53:55...'`
     - Clean warning logged, retry scheduled, zero unhandled exceptions or container crashes.
  3. New-bot topic creation:
     - `broadcaster.ensure_topic()` catches `UserAgentSessionUnavailableError` from `user_agent.find_topic_id()`, raises `RuntimeError("Could not resolve existing topic: No usable user session")`.
     - Honeypot broadcast catches topic failure and records `_mark_broadcast_failure(..., BroadcastSendError("topic_failed", ..., retryable=True))`.
     - Celery queues retry for when user sessions recover; no silent drop and no crashes.
- Root cause:
  1. Celery Beat dispatched `honeypot_redirect_sweep` every 30s. With 2 workers, sweep tasks piled up into hundreds in Redis.
  2. Tasks waiting in queue had valid claim tokens, but `_is_fence_valid()` enforced an arbitrary 10-minute timeout (`gte("redirect_claimed_at", now - 10m)`). Tasks that waited > 10m were rejected with `fenced_out`.
  3. On failures (including dead bot tokens returning HTTP 401 like `@monkeydatebot`), `_honeypot_redirect_one_logic` reset `redirected_at = None` and `redirect_claimed_at = None`, while the 15-minute sweeper cleared stale claims and re-dispatched the same row indefinitely (over 9,675 failed attempts for `@monkeydatebot` alone).
- Fix:
  1. Added a non-blocking Redis lock (`theprawnhunter:lock:honeypot_redirect_sweep`, 25s TTL) and 15s timestamp debounce to `honeypot_redirect_sweep` so duplicate backlogged sweeps exit in ~10ms without queuing duplicate work.
  2. In `_is_fence_valid`, removed the restrictive 10-minute age cutoff so queue wait time no longer falsely marks in-flight jobs as `fenced_out`.
  3. In `_honeypot_redirect_one_logic` and `_honeypot_redirect_sweep_logic`:
     - Dead tokens (HTTP 401 / Unauthorized): logs dead-token skip, caches in Redis `credential:revoked:{cred_id}`, calls `_mark_credential_revoked` to update DB status to `revoked`, and finalizes the row in `honeypot_updates` with `redirected_at = now, redirected_bot = 'token_revoked', redirect_error = redirect_error`.
     - Blocked users (HTTP 403 / Forbidden): finalizes row with `redirected_at = now, redirected_bot = 'user_blocked'`.
     - Max retries (attempts >= 3): finalizes row with `redirected_at = now, redirected_bot = 'max_retries_exceeded'`.
     - In-flight marker (`redirect:inflight:{update_id}`) prevents double-dispatch while queued.
- Concurrency evaluation:
  - Base `docker-compose.yml` sets `--concurrency=${WORKER_CONCURRENCY:-2}` while `docker-compose.prod.yml` sets 6.
  - Live inspection confirmed concurrency 2 is NOT the bottleneck once duplicate loops are broken: queue drained rapidly from 796 down to 510 in minutes. Concurrency left unchanged at 2.
- Rebuild & Live Verification:
  - Rebuilt ONLY `theprawnhunter_worker` container (`docker compose up -d --build --no-deps worker`).
  - Unit tests: 12/12 passed in `tests/unit/test_honeypot_redirect_bugs.py`.
  - Queue depth: dropped from 796 to 510 and steadily draining.
  - Sweeper: backlogged sweep tasks drained in ~0.012s each with `{'status': 'skipped', 'reason': 'debounced'}`.
  - Fenced_out churn: exactly 0 new `fenced_out` occurrences in live worker logs over 10-minute observation.
  - Dead tokens: `@monkeydatebot` jobs skipped and finalized, not retried.

### Fix 1 evidence — 2026-10-14
- Root cause: `theprawnhunter_api` container had Docker-assigned upstream DNS `100.100.100.100` (Tailscale MagicDNS) in `/etc/resolv.conf`, which returned `RCODE 2` (Server Failure) for domain resolution. As a result, all database writes to Supabase in `app/api/routers/honeypot.py` threw `[Errno -3] Temporary failure in name resolution` and responded `HTTP 500` to Telegram (13,955 failures in 24h).
- Fix: Added explicit public DNS servers (`8.8.8.8`, `1.1.1.1`) to the `api` service definition in `docker-compose.yml`.
- Rebuild: Rebuilt ONLY `theprawnhunter_api` container (`docker compose up -d --build api`).
- Live Verification:
  - DNS resolution verified inside container: `socket.gethostbyname("api.telegram.org")` -> `149.154.166.110`.
  - Supabase connectivity verified inside container: `db.table("honeypot_updates").select(...)` succeeded.
  - Real inserts verified live: row count in `honeypot_updates` immediately climbed from 202 to 222+ as live webhooks landed.
  - Error rate: exactly 0 errors / 0 HTTP 500s logged since container startup.
  - Telegram pending updates draining: `@uhkls3bot` drained 2 -> 0; `@Scampage2_bot` drained 6 -> 0; `@poiu4bot` drained 6 -> 2; `@iamprcompanyBot` drained 7 -> 4.

## Previous task — Ralph loop: tail work (started 2026-10-14)

### Item 4 evidence — 2026-10-14

- GitHub Dependabot alert #58 was the sole open moderate alert: `pytest` in `requirements-dev.txt`, GHSA-6w46-j5rx-g56g / CVE-2025-71176, vulnerable range `<9.0.3`, patched version `9.0.3`.
- Updated `requirements-dev.txt` from pytest 8.3.5 / pytest-asyncio 0.24.0 to pytest 9.0.3 / pytest-asyncio 1.4.0, matching the already-pinned compatible versions in `requirements.txt`. Dependency fix commit `889f766` was pushed to `main`.
- After the push, GitHub's dependency graph SBOM showed pytest 9.0.3 and Dependabot alert #58 changed to `fixed` at `2026-10-14T02:08:22Z`.
- Ran `./.venv/bin/python -m pytest tests/unit/` under pytest 9.0.3 + pytest-asyncio 1.4.0: 566 passed, 2 failed (568 total). The failures were `test_telemetry_command_restricts_non_admin` (expected reply text differs from `Admin only.`) and `test_log_calls_never_interpolate_raw_telegram_user_ids` (found three source locations); neither touches the dependency files changed here. No application code was changed.

### Item 3 evidence — 2026-10-14

- The redirect detail fix is commit `5a370ed` (2026-10-13 23:09:43 SGT / 15:09:43 UTC). Queried live `honeypot_updates` rows with non-null `redirect_error` and `received_at` on or after that UTC time: 3 rows, received from 2026-10-13 15:48:20 to 19:04:22 UTC. All 3 contain `HTTP 403: Forbidden: bot was blocked by the user`.
- Queried live worker logs from the fix time onward: 491 `[Redirect] FAILED` entries, spanning 2026-10-14 00:18:26–01:59:51 local. All carry the same HTTP status and Telegram error text. The answer is: **HTTP 403 — Forbidden: bot was blocked by the user.**
- This is a live Telegram response; no failure was fabricated. No code or credentials were changed.

### Item 2 evidence — 2026-10-14

- The live worker's source contains the `scrape_history` takeover preflight and `takeover_only` path from commit `867f25d`; runtime config has honeypot mode, receiver, secret, and AUTO allowlist enabled, and the target credential passes the allowlist check.
- `docker logs --since 48h theprawnhunter_worker`: credential `e34b2ee0-ddcc-4479-bd9f-491a051d1ee5` (`@notifyGitlabRepobot`) began exfiltration at 00:52:40 UTC. At 00:52:51 UTC the worker logged a foreign webhook, then `setWebhook succeeded ... (foreign webhook re-taken over)`, and continued into the scraper flow. At 00:52:52 UTC, the scrape audit recorded `bot_api_updates` with `getUpdates_status=409` and `honeypot_active=true`; the scrape completed as `no_new_messages`.
- The foreign-webhook URL in the raw worker log contains a Telegram Bot API token. The value is redacted in this record. No credential changes or unrelated code changes were made.
- Diagnosis: `.agents/diagnosis/2026-10-14-notifygitlabrepobot-retake.md`.

## Current task (updated 2026-10-14) — remaining 2 findings DONE; worker rebuilt + verified healthy; log watch clean (0 errors, 0 redirect failures)

## Previous current task (updated 2026-10-14)

- **Job:** Fix hidden backfill failures, then check whether overlapping runs can update the same rows.
- **Status:** [x] Part A: the Celery backfill task now retries exceptions, stores its result, and the test confirms a failed database update enters Celery retry and the retry processes the unchanged row. Focused tests: 8 passed, 0 failed.
- **Status:** [x] Part B: stale candidates could be updated again after the broadcaster made a row eligible. Apply runs now use a shared Redis lock; the overlap regression passes. Focused tests after both parts: 9 passed, 0 failed.
- **Next:** None; both fix commits are pushed and verified on `origin/main`.

## Previous task — scanner hardening (completed)

- **Job:** Check and harden all 12 bot-discovery scanners. One checkbox at a time. No jumping ahead, no wandering. SOLE WRITER on this repo — the 3-fix job has stood down; its Fixes 2–3 wait until this job fully completes.
- **Status:** ALL 12 SCANNERS DONE + F1/F2 done. Vet-and-harden job COMPLETE. Next: Fixes 2-3 (3-fix job resumes).
- **Blocked (2026-10-14 ~03:42 SGT):** 3410 cannot resolve hostnames. Tailscale DNS 100.100.100.100 does not answer (Python resolver: Name or service not known). IP connectivity works (ping 8.8.8.8 0% loss; curl to GitHub by direct IP returns 200). Codex cannot reach its backend and pushes cannot reach GitHub until name resolution recovers. Supervisor did NOT change any network/DNS settings (read-only diagnosis only — fixing DNS on the production machine needs Bryan's call). Resume: when name resolution works again, relaunch scanner 11 with /tmp/codex-scanner-11-telegram-prompt.md, then scanner 12, then supervisor does F1 rebuild + F2 log watch.
- **Next-step:** Vet job complete. Supervisor reports; Fixes 2-3 resume next.
- **Discipline (Bryan):** After EVERY small task, update STATE.md (tick the checkbox, set next step) AND append a JOURNAL.md line (what was done, why, commit hash). No step is done until both files are updated. Commit STATE.md promptly with each fix commit so it cannot be clobbered.
- **Task list — every task small enough to finish in one step:**
  - [x] S1.1 Vet GitHub search — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S1.2 Harden GitHub search — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S1.3 Test GitHub search — failure paths covered with REAL code, no fake stand-ins.
  - [x] S1.4 Commit GitHub search — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S2.1 Vet GitHub firehose — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S2.2 Harden GitHub firehose — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S2.3 Test GitHub firehose — failure paths covered with REAL code, no fake stand-ins.
  - [x] S2.4 Commit GitHub firehose — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S3.1 Vet Shodan — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S3.2 Harden Shodan — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S3.3 Test Shodan — failure paths covered with REAL code, no fake stand-ins.
  - [x] S3.4 Commit Shodan — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S4.1 Vet URLScan — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S4.2 Harden URLScan — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S4.3 Test URLScan — failure paths covered with REAL code, no fake stand-ins.
  - [x] S4.4 Commit URLScan — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S5.1 Vet GrepApp — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S5.2 Harden GrepApp — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S5.3 Test GrepApp — failure paths covered with REAL code, no fake stand-ins.
  - [x] S5.4 Commit GrepApp — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S6.1 Vet Gist — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S6.2 Harden Gist — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S6.3 Test Gist — failure paths covered with REAL code, no fake stand-ins.
  - [x] S6.4 Commit Gist — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S7.1 Vet Exa — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S7.2 Harden Exa — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S7.3 Test Exa — failure paths covered with REAL code, no fake stand-ins.
  - [x] S7.4 Commit Exa — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S8.1 Vet Rentry — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S8.2 Harden Rentry — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S8.3 Test Rentry — failure paths covered with REAL code, no fake stand-ins.
  - [x] S8.4 Commit Rentry — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S9.1 Vet Hastebin — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S9.2 Harden Hastebin — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S9.3 Test Hastebin — failure paths covered with REAL code, no fake stand-ins.
  - [x] S9.4 Commit Hastebin — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S10.1 Vet Wayback — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S10.2 Harden Wayback — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S10.3 Test Wayback — failure paths covered with REAL code, no fake stand-ins.
  - [x] S10.4 Commit Wayback — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S11.1 Vet Telegram search — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S11.2 Harden Telegram search — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S11.3 Test Telegram search — failure paths covered with REAL code, no fake stand-ins.
  - [x] S11.4 Commit Telegram search — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] S12.1 Vet Netlas — read the code; check what happens when the provider is down, errors, rate-limits, or returns nothing.
  - [x] S12.2 Harden Netlas — every failure logged clearly; no whole-run crash; work stays retryable next cycle.
  - [x] S12.3 Test Netlas — failure paths covered with REAL code, no fake stand-ins.
  - [x] S12.4 Commit Netlas — commit + push + tick this box + JOURNAL.md line with commit hash.
  - [x] F1 Rebuild ONLY the worker container, verify healthy.
  - [x] F2 Log watch — brief watch, no new crashes.
- **Rules for every scanner:** No port changes, no database changes, no login/secret changes. Do NOT change scan times (the 4h/6h/12h rhythm stays as-is).
- **Supervision rules (Bryan 2026-10-13):** small steps, one at a time, no wandering; update STATE.md + JOURNAL.md after EACH small step; liveness checks every few minutes (process alive, log growing, deliverable moved); on crash, verify state from scratch and resume from STATE.md checkpoint, never restart from zero. Tests must use REAL code, no fake stand-ins.
- **How this job runs:** One Codex run per scanner, one commit per scanner. The supervisor starts each run, watches it, checks the commit landed, then starts the next.

Last updated: 2026-09-18 (prawn-ui batch)

## Repo identity
- Owner: hongyime
- Remote: https://github.com/hongyime/theprawnhunter.git
- Upstream fork: https://github.com/0x6rss/matkap.git
- Branch: maintenance/prawn-ui-20260916 (reused for follow-up chores after PR #21 merged)
- License: Apache-2.0

## Stack
- Backend: Python 3.11 (FastAPI + Celery + Redis + Supabase Postgres) — 10 Docker Compose services
- Frontend: Next.js (frontend/), TypeScript, Vitest, deployed to Vercel (project `theprawnhunter`)
- Extension: Manifest V3 Chrome extension (extension/)
- Test: pytest + pytest-asyncio (Python), vitest (frontend)
- Root package.json is a thin shim (only `puppeteer-core` + FOFA scan scripts). Real workloads live in `frontend/package.json` and Python.

## Deployment
- Vercel project: `theprawnhunter` (projectId: prj_uhn1QEFSTzn33kI2FFXpscgyqt6n, orgId: team_ARK7HKobyCMp0PCArQTLxbz6)
- Docker Compose for the backend stack (docker-compose.yml, docker-compose.prod.yml)
- CI: GitHub Actions — ci.yml, bandit.yml, semgrep.yml, trufflehog.yml, supabase-keep-alive.yml, labeler.yml, greetings.yml, operator-authorization.yml

## Vercel / frontend-linking status ("unlinked frontend source")
- Both `.vercel/project.json` (repo root) AND `frontend/.vercel/project.json` exist locally, each pointing to the SAME Vercel project (prj_uhn1QEFSTzn33kI2FFXpscgyqt6n).
- `.vercel/` is gitignored (see .gitignore line 81: `.vercel`), so neither file is tracked. They are per-checkout artifacts of `vercel link`.
- Frontend build config (`frontend/vercel.json`) is correct: framework=nextjs, buildCommand=`npm run typecheck && npm run build`, outputDirectory=`.next`, region=iad1.
- The remediation-checkout ambiguity: whichever directory `vercel deploy` is invoked from will win. Best practice going forward: run `vercel` only from `frontend/`, and delete the stray root `.vercel/` locally.
- No repo-side fix is required (both are gitignored); this is a per-checkout workflow discipline issue.

## Open issues / PRs
- Open issues: 0
- Open PRs: 0 (PR #21 merged 2026-09-17T23:08:16Z, merge commit a85decc; already in `main`)

## Recent activity
- 2026-09-18: Prawn UI visual style applied to frontend (PR #21: `maintenance/prawn-ui-20260916`)
- 2026-09-12 → 2026-09-14: operator-authorization guard hardening (PR #20 merged: `maintenance/operator-guard-20260912`)
- CI: labeler PR label access fix (e7c30d6), labeler config loading fix (b9475ec)
- Tests: auth control differentiation (3ae15d2)
- Docs: README + PRD rewrite for `03-document`

## Known local branches (unpushed)
- `maintenance/check-bootstrap-20260914` (local only)
- `shell/standardise` (local only)
- `remediation/2026-09-06` (has remote counterpart)

## Free-tier compliance
- Supabase Free (500 MB DB — README notes Pro recommended for sustained use; caller responsibility)
- Vercel Hobby: single project deploy from `frontend/`
- Docker Compose is self-hosted (not counted against Vercel or Supabase quotas)

## Gaps / follow-ups
- No root-level `AGENTS.md` — the task expectation "AGENTS.md exists" is unmet. Deferred: creating one is beyond a "targeted low-risk fix" and requires product-owner decisions on conventions.
- `.agents/` currently only holds `diagnosis/` and `tickets/` subdirectories; STATE.md + JOURNAL.md added in this session.

## Docker Compose minimal-footprint reorg (this session)

- Rewrote `docker-compose.yml`: 4 split celery workers (worker-core/scanners/scrape/validators, 2 CPU/2g each) consolidated into ONE combined `worker` consuming all 4 queues (celery,scanners,scrape,validation) at 1 CPU/768m -- scale horizontally with `--scale worker=N` or `WORKER_CONCURRENCY` instead of separate containers. `bot`, `flower`, `frontend` moved behind Compose `profiles` (`bot`/`monitoring`/`frontend`/`full`) so the DEFAULT `docker compose up -d --build` now starts only redis+api+worker+beat (the minimal dev stack) -- the other three services are opt-in. Added shared `x-logging`/`x-app-build` YAML anchors (DRY). Tightened every service's resource knobs down (redis 0.5cpu/1280m->0.25cpu/320m; api 2cpu/2g->1cpu/512m; worker 2cpu/2g x4 services->1cpu/768m x1; etc.) and shortened healthcheck timeouts to match the smaller footprint. `restart: always` -> `restart: unless-stopped` throughout (won't auto-restart a manually-stopped dev container). Single explicit network (`theprawnhunter_net`) and the 4 legacy-prefixed external volumes are unchanged -- no data migration needed.
- Updated `docker-compose.prod.yml` overlay to match: single `worker` override (concurrency 6 instead of the old per-queue 8/8/6/16 overrides across 4 services).
- Validated with `docker compose config --quiet` (base), `--profile full config --quiet`, and `-f docker-compose.yml -f docker-compose.prod.yml --profile full config --quiet` -- all exit 0. No container was started; no image was built; no `docker compose up` was run at any profile or overlay.
- Docker Desktop state checked before and after: zero theprawnhunter containers/networks exist locally (matches the user's report of having deleted the stack). All 4 required external volumes (`telegramhunter_redis_data`, `_sessions`, `_imports`, `_beat_schedule`) are still present and intact -- `down`/container deletion does not remove externally-named volumes, so there is no data loss to rectify. One unrelated, unnamed, auto-generated container (`ecstatic_thompson`, image `35d01a69f9fd`, running an `apt-get` shell) was observed transiently on this shared machine during this session and exited/was removed on its own before any action was taken on it -- confirmed unrelated to theprawnhunter (no `theprawnhunter_*` name, no theprawnhunter image hash) and left untouched throughout.
- Merged this branch directly into `main` (fast-forward, commit `b11d174`) and pushed both -- `main` and `maintenance/prawn-ui-20260916` are now identical (0/0 divergence). PR-creation via the GitHub API MCP tool failed with an auth-scope error (same limitation observed on other repos this session); given the user's explicit "merge to main if needed" authorization and the fully-validated, docs+declarative-YAML-only diff, merged and pushed directly via git instead of leaving an open PR -- `main` accepted the push with no branch-protection rejection.

## Reviewed workspace maintenance - 2026-09-27

Publish the reviewed portability and privacy maintenance from the current default branch, preserving concurrent upstream work and original workspace changes. Validation is limited to the documented offline fixtures and hosted checks; no live data job or deployment command was executed locally.

2026-09-27: Runtime, development-launcher, and publishing enhancements were withdrawn after safety review. Only privacy and defensive maintenance remain in scope. No image build, runtime deployment, publication, commit, or push was performed.

<!-- MOLT_AUTO_START -->
## Auto State

- Updated: 2026-09-28 22:08:20 +08:00
- Machine: PRAWN-E14
- Harness: claude
- Event: stop
- Branch: main
- HEAD: 2d5becb
- Dirty files: 0
- Resume hint: Read .agents/STATE.md, then the latest file in .agents/handoffs/ if present.
<!-- MOLT_AUTO_END -->

## Checkpoint 2026-10-13 23:12 SGT — redirect error fix (resumed)

- Fix verified on disk; 9/9 tests pass (supervisor-run). Next: commit, push, rebuild worker, 10-min log watch.

## Completed check — login cleanup import

- **Current task:** Verify the reported missing `schedule_deletion` import in `app/services/_bot_listener/login.py`.
- **Progress:** Already fixed on `main`; line 4 imports it from `.commands`, where the helper is defined. Commit `cb8ed1c0f235e0f0545c5119f77e37e5124d5956` adds that import. No code change or tests were needed.
- **Next step:** No follow-up for this check; continue with the existing next task when directed.
