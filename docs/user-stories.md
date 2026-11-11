# User-story review record

## Review rules

- Code is the source of truth for actual behavior.
- Product intent comes from the project owner; no behavior is treated as intended until confirmed.
- Application code and configuration are not changed during review without explicit approval of a proposed fix.

## Phase 1 — project map

Status: mapped on 2026-11-08. No application code changed. Feature selection pending.

### System and entry points

The Prawn Hunter is a self-hosted OSINT and operations system. It discovers Telegram Bot API tokens from public sources, validates and enriches them, collects accessible evidence, and presents the results through a private Telegram group and a Supabase-backed web dashboard. The deployable stack is a FastAPI service, Redis, Celery worker/beat services, an unprofiled SearXNG service, and opt-in bot, Flower, dashboard, and heavy-media services ([README.md](../README.md#L1), [docker-compose.yml](../docker-compose.yml#L41)).

Entry points:

- Browser: Next.js pages at `/`, `/signin`, and `/about`; the signed-in home page switches among the nine dashboard views ([frontend/app/page.tsx](../frontend/app/page.tsx#L18), [frontend/components/Sidebar.tsx](../frontend/components/Sidebar.tsx#L52)).
- HTTP API: `app.api.main:app` mounts monitor, scan, ingest, health, media, and honeypot routers ([app/api/main.py](../app/api/main.py#L191)).
- Background work: Celery runs scheduled discovery, validation, collection, alerting, reporting, maintenance, and scanner tasks in UTC ([app/workers/celery_app.py](../app/workers/celery_app.py#L209)).
- Telegram: the optional bot listener registers public and administrator command handlers plus the private `/starthunter` login conversation ([app/services/_bot_listener/runtime.py](../app/services/_bot_listener/runtime.py#L220)).
- Chrome extension: a Manifest V3 FOFA scraper can scan, validate, and upload results to the API using a configured monitor key ([extension/manifest.json](../extension/manifest.json#L1), [extension/background.js](../extension/background.js#L851)).
- Operator CLIs: deployment validation/migration scripts plus report, outpost, backfill, cleanup, session, and FOFA tools under `scripts/deploy/` and `scripts/ops/`; destructive recovery tools commonly require explicit `--apply` ([README.md](../README.md#L407)).

### Actors and access boundaries

| Actor | What code lets it do |
|---|---|
| Public visitor | View the marketing/about page and the sign-in page; an unauthenticated visit to the dashboard is directed to sign in ([frontend/app/page.tsx](../frontend/app/page.tsx#L123)). |
| Signed-in non-operator | Can load the dashboard shell, but Supabase RLS and server proxies deny protected data/actions unless the JWT has `app_metadata.operator: true` ([supabase/migrations/20260912121145_restrict_dashboard_operator_access.sql](../supabase/migrations/20260912121145_restrict_dashboard_operator_access.sql#L3), [frontend/lib/operator-auth.ts](../frontend/lib/operator-auth.ts#L22)). |
| Dashboard operator | Uses the dashboard to review findings/evidence, search, manage watchlists, view targets/chat/telemetry/metrics, and pause/resume the system ([frontend/components/Sidebar.tsx](../frontend/components/Sidebar.tsx#L52)). |
| Monitor API client | Uses `X-Monitor-Key` to call `/monitor`, `/ingest`, `/scan`, protected health, and media APIs. A missing/wrong key is rejected; a missing configured key fails closed ([app/core/auth.py](../app/core/auth.py#L27)). |
| Telegram group administrator | Uses administrator-only bot commands for runtime control, account onboarding, data retrieval, backfill, telemetry, and watchlists ([app/services/_bot_listener/commands.py](../app/services/_bot_listener/commands.py#L69)). |
| Telegram private-chat user | May start the account-login conversation only when permitted by the admin/allow-public policy ([app/core/config.py](../app/core/config.py#L38), [app/services/_bot_listener/runtime.py](../app/services/_bot_listener/runtime.py#L252)). |
| Chrome-extension operator | Configures a FOFA query, API URL, and monitor key locally, runs scans, and uploads the discovered results ([extension/ui/popup.html](../extension/ui/popup.html#L35), [extension/background.js](../extension/background.js#L869)). |
| Scheduled worker | Executes periodic scanner, validation, scrape, finding, alert, retention, telemetry, and operations tasks ([app/workers/celery_app.py](../app/workers/celery_app.py#L299)). |
| External source/service | Public search sources, Telegram APIs, Supabase, Redis, optional alert webhook, and the honeypot webhook receiver feed or support the system. Honeypot receiving is gated by runtime configuration ([app/api/routers/honeypot.py](../app/api/routers/honeypot.py#L75)). |
| Remote validation outpost | When explicitly enabled, leases plaintext validation candidates, validates them independently, and reports idempotent results ([app/api/routers/_monitor/outposts.py](../app/api/routers/_monitor/outposts.py#L1)). |

### Feature map

| Flow | What a user/actor can do | Main implementation | Actors |
|---|---|---|---|
| Discover and queue token candidates | Scheduled scanners and the FOFA extension find candidate tokens; the API validates basic shape, deduplicates, encrypts, stores, and queues enrichment. | `app/workers/tasks/scanners/`, `extension/`, [app/api/routers/ingest.py](../app/api/routers/ingest.py#L58) | Scheduled worker, extension operator, monitor API client |
| Manually ingest tokens / trigger scans | An API client can paste token lists or queue a supported scanner in non-production environments. | [app/api/routers/ingest.py](../app/api/routers/ingest.py#L240), [app/api/routers/scan.py](../app/api/routers/scan.py#L15) | Monitor API client |
| Validate, enrich, and collect evidence | Worker tasks validate candidates, establish chat/bot context, collect accessible messages/media, and periodically re-scrape active targets. | `app/workers/tasks/validation_tasks.py`, `app/workers/tasks/flows/scrape.py`, [app/workers/celery_app.py](../app/workers/celery_app.py#L319) | Scheduled worker |
| Findings and evidence triage | An operator reviews prioritized findings, reads redacted/raw drill-down evidence, and records feedback/disposition. | [frontend/components/FindingsQueue.tsx](../frontend/components/FindingsQueue.tsx#L344), [app/api/routers/_monitor/findings.py](../app/api/routers/_monitor/findings.py#L7) | Dashboard operator, monitor API client |
| Search and target drill-down | An operator full-text or semantic-searches evidence, filters target records, then opens a chat feed or bot telemetry. | [frontend/components/SearchView.tsx](../frontend/components/SearchView.tsx#L270), [frontend/components/Sidebar.tsx](../frontend/components/Sidebar.tsx#L359), [frontend/app/api/semantic-search/route.ts](../frontend/app/api/semantic-search/route.ts#L42) | Dashboard operator |
| Chat evidence, exports, and media | An operator browses paged redacted evidence, searches a target, exports chat rows, views stored thumbnails, and an API client can proxy original Telegram media with a monitor key. | [frontend/components/ChatWindow.tsx](../frontend/components/ChatWindow.tsx#L76), [app/api/routers/media.py](../app/api/routers/media.py#L16), [app/api/routers/_monitor/exports.py](../app/api/routers/_monitor/exports.py#L7) | Dashboard operator, monitor API client |
| Watchlists and hit alerts | An operator creates/edits/toggles/deletes literal or regex rules, inspects matching evidence, and opens a Telegram deep link when resolvable. | [frontend/components/WatchlistView.tsx](../frontend/components/WatchlistView.tsx#L65), [frontend/components/WatchlistHitsView.tsx](../frontend/components/WatchlistHitsView.tsx#L75), [supabase/migrations/20261025000004_watchlists.sql](../supabase/migrations/20261025000004_watchlists.sql#L1) | Dashboard operator, Telegram administrator, scheduled worker |
| Telemetry and metrics | An operator views bot/global indicators and historical acquisition, message, takeover, and health metrics. | [frontend/components/TelemetryAnalyticsView.tsx](../frontend/components/TelemetryAnalyticsView.tsx#L40), [frontend/components/DashboardView.tsx](../frontend/components/DashboardView.tsx#L280) | Dashboard operator |
| Runtime operations | An operator checks queue/pause status and pauses/resumes new work through an authenticated server-side proxy; a Telegram administrator can use equivalent commands. | [frontend/components/OpsPanel.tsx](../frontend/components/OpsPanel.tsx#L35), [frontend/app/api/ops/[action]/route.ts](../frontend/app/api/ops/%5Baction%5D/route.ts#L110), [app/api/routers/_monitor/ops.py](../app/api/routers/_monitor/ops.py#L174) | Dashboard operator, Telegram administrator, monitor API client |
| Telegram control plane | An administrator obtains health/status, pauses/resumes, manages sessions, requests attachments, runs safe/dangerous backfills, and manages watchlists from Telegram. | [app/services/_bot_listener/runtime.py](../app/services/_bot_listener/runtime.py#L230), [app/services/_bot_listener/watchlist_commands.py](../app/services/_bot_listener/watchlist_commands.py#L1) | Telegram administrator |
| Broadcast and alert delivery | Workers deliver enabled raw evidence to forum topics, create/send finding and watchlist alerts, and send operational logs; raw broadcast and finding webhooks have separate gates. | `app/workers/tasks/flows/broadcast.py`, `app/services/finding_alerts.py`, [app/core/config.py](../app/core/config.py#L121) | Scheduled worker, Telegram group |
| Honeypot and redirects | When enabled/configured, the API receives taken-over-bot webhook updates, stores evidence, and background tasks can send gated redirect/outreach sequences. | [app/api/routers/honeypot.py](../app/api/routers/honeypot.py#L75), [app/workers/celery_app.py](../app/workers/celery_app.py#L432) | External Telegram webhook, scheduled worker |
| Health, audit, maintenance, and remote validation | API clients can inspect health/queues/quotas and maintenance endpoints; workers retain data, reconcile state, audit sessions; optional outposts can validate leases. | [app/api/routers/health.py](../app/api/routers/health.py#L230), [app/workers/celery_app.py](../app/workers/celery_app.py#L515), [app/api/routers/_monitor/outposts.py](../app/api/routers/_monitor/outposts.py#L94) | Monitor API client, scheduled worker, remote outpost |

### Feature groups — detailed behavior observed

These are descriptive maps, not confirmed user stories. They distinguish an on-screen user action from API, bot, extension, and scheduled-worker-only behavior.

#### 1. Discovery and ingestion

- A Chrome-extension operator can set a FOFA query, domain mode, scan cadence, API URL, and monitor key; they can start, stop, resume, and upload a scan from its popup ([extension/ui/popup.html](../extension/ui/popup.html#L25), [extension/ui/popup.js](../extension/ui/popup.js#L74)).
- At upload, the extension sends every collected result—not only locally marked valid ones—to `POST /ingest/extension/credentials`, with the monitor key when configured ([extension/background.js](../extension/background.js#L856)).
- A monitor API client can also paste newline/comma-delimited tokens or JSON tokens to `POST /ingest/tokens`; there is no dashboard screen for this operation ([app/api/routers/ingest.py](../app/api/routers/ingest.py#L240)).
- Both ingest paths reject malformed token shapes and configured own-bot tokens, deduplicate within a submitted batch by SHA-256 hash, store the token through the encryption service, and try to queue validation/enrichment ([app/api/routers/ingest.py](../app/api/routers/ingest.py#L70), [app/api/routers/ingest.py](../app/api/routers/ingest.py#L282)).
- Scheduled scanners run at staggered intervals; some sources require credentials, and GitLab/Pastebin schedules are explicitly disabled despite provider code being present ([app/workers/celery_app.py](../app/workers/celery_app.py#L543)).
- Separately, a keyed API client can queue a limited set of manual scanner tasks, but only outside production ([app/api/routers/scan.py](../app/api/routers/scan.py#L15)).

#### 2. Validation and evidence capture

- The system has no end-user collection button in the dashboard. Ingestion/scanners feed worker queues; workers validate candidates, enrich records, and schedule initial and periodic active-target collection ([app/api/routers/ingest.py](../app/api/routers/ingest.py#L169), [app/workers/celery_app.py](../app/workers/celery_app.py#L319)).
- A validation candidate can be handed to an outpost only when `OUTPOST_ENABLED` is true. A keyed outpost receives a short-lived lease with plaintext candidates, and replayed results are designed to be a no-op rather than increment attempts twice ([app/api/routers/_monitor/outposts.py](../app/api/routers/_monitor/outposts.py#L94), [app/api/routers/_monitor/outposts.py](../app/api/routers/_monitor/outposts.py#L153)).
- Media archiving, heavy OCR/ASR, re-scraping, and user-agent membership maintenance are worker/configuration controlled rather than dashboard actions ([app/core/config.py](../app/core/config.py#L79), [app/workers/celery_app.py](../app/workers/celery_app.py#L490)).

#### 3. Findings triage

- The operator’s initial dashboard view is Findings. It displays priority-ranked records whose default backend statuses are `new`, `triaged`, or `in_progress`; resolved, dismissed, and suppressed records are hidden unless explicitly requested through the monitor API ([frontend/app/page.tsx](../frontend/app/page.tsx#L65), [app/api/routers/_monitor/findings.py](../app/api/routers/_monitor/findings.py#L7)).
- The operator can expand a finding, load evidence, retry failed evidence loads, drill into its chat or telemetry, and mark it Useful, Noise, Start work, or Resolve. Those UI actions call an operator-gated feedback RPC and can change status ([frontend/components/FindingsQueue.tsx](../frontend/components/FindingsQueue.tsx#L450), [frontend/components/FindingsQueue.tsx](../frontend/components/FindingsQueue.tsx#L877)).
- The monitor API presents redacted finding evidence; the dashboard has a separate raw-evidence path, subject to operator database authorization ([app/api/routers/_monitor/findings.py](../app/api/routers/_monitor/findings.py#L38), [frontend/components/FindingsQueue.tsx](../frontend/components/FindingsQueue.tsx#L211)).
- A scheduled producer creates/reconciles findings and routes alert deltas, so new findings appear without a dashboard action ([app/workers/celery_app.py](../app/workers/celery_app.py#L399)).

#### 4. Search and target drill-down

- The dashboard offers evidence search and a target list. The target list loads up to 2,000 credential summaries, supports status/chat-type filters, does browser-side matching for a short query, and uses a server RPC after two characters ([frontend/components/Sidebar.tsx](../frontend/components/Sidebar.tsx#L22), [frontend/components/Sidebar.tsx](../frontend/components/Sidebar.tsx#L428)).
- Evidence search supports full-text search and a Semantic toggle. The semantic request goes through a server-only proxy that verifies the caller’s operator claim and holds the monitor key outside the browser ([frontend/components/SearchView.tsx](../frontend/components/SearchView.tsx#L270), [frontend/app/api/semantic-search/route.ts](../frontend/app/api/semantic-search/route.ts#L42)).
- The keyed monitor API rejects full-text queries under three characters or outside its restricted character/length pattern; FTS falls back to a slower substring query if the FTS RPC is unavailable, and an unavailable fallback returns an empty/degraded response rather than an HTTP 500 ([app/api/routers/_monitor/search.py](../app/api/routers/_monitor/search.py#L154)).
- Semantic search returns a fallback result when the embedding service, response, or semantic-search RPC is unavailable; the dashboard can then retry its FTS path ([app/api/routers/_monitor/search.py](../app/api/routers/_monitor/search.py#L299)).
- Search results and target selections can open a credential’s chat feed or bot telemetry; there is no edit/delete target capability in the dashboard ([frontend/app/page.tsx](../frontend/app/page.tsx#L83)).

#### 5. Chat evidence, exports, and media

- Selecting a target opens its Chat Feed. The feed reads `evidence_redacted`, initially fetches the newest page, supports loading older pages, and merges new rows by ID so a poll does not replace older loaded history ([frontend/components/ChatWindow.tsx](../frontend/components/ChatWindow.tsx#L173), [frontend/components/ChatWindow.tsx](../frontend/components/ChatWindow.tsx#L321)).
- The feed subscribes to a private evidence event when available; it polls as a fallback, and polling pauses for hidden browser tabs ([frontend/components/ChatWindow.tsx](../frontend/components/ChatWindow.tsx#L243)).
- Within a selected chat, the operator can search that credential’s evidence, export visible chat data as JSON or CSV, inspect media/gallery items, and inspect source/profile details ([frontend/components/ChatWindow.tsx](../frontend/components/ChatWindow.tsx#L379), [frontend/components/ChatWindow.tsx](../frontend/components/ChatWindow.tsx#L695)).
- The code provides a keyed `/media/{message_id}` API that decrypts the source token server-side and retrieves the file from Telegram; it responds with 404 for absent rows/metadata and hides decryption details. Its exact relationship to every dashboard media link should be verified in the later chat/media story rather than inferred here ([app/api/routers/media.py](../app/api/routers/media.py#L16)).
- The monitor API also offers export endpoints, but the dashboard export action is client-side; their filtering/format scope should be reviewed separately if export behavior is important ([app/api/routers/_monitor/exports.py](../app/api/routers/_monitor/exports.py#L7)).

#### 6. Watchlists and hit alerts

- The dashboard lets an operator create a named literal or regex pattern, optionally scope it to one credential UUID, enable/disable it, set a cooldown, edit it, and delete it with confirmation ([frontend/components/WatchlistView.tsx](../frontend/components/WatchlistView.tsx#L165), [frontend/components/WatchlistView.tsx](../frontend/components/WatchlistView.tsx#L277)).
- Client validation requires a name and pattern, limits patterns to 200 characters, requires a cooldown of at least one minute, and validates an optional credential UUID. Database RPCs enforce the operator claim and protect direct table access ([frontend/components/WatchlistView.tsx](../frontend/components/WatchlistView.tsx#L137), [supabase/migrations/20261025000004_watchlists.sql](../supabase/migrations/20261025000004_watchlists.sql#L72)).
- The Watchlist Hits view loads recent matching evidence, filters by rule name, and can ask a server-side proxy to resolve/open the matching Telegram message; a missing group/topic/message link remains unavailable instead of being guessed ([frontend/components/WatchlistHitsView.tsx](../frontend/components/WatchlistHitsView.tsx#L75), [frontend/app/api/watchlist-hit-link/route.ts](../frontend/app/api/watchlist-hit-link/route.ts#L36)).
- Workers backfill watchlist matches hourly and drain queued watchlist alerts every two minutes. Telegram bot administrators also have a `/watchlist` command surface ([app/workers/celery_app.py](../app/workers/celery_app.py#L303), [app/workers/celery_app.py](../app/workers/celery_app.py#L517), [app/services/_bot_listener/watchlist_commands.py](../app/services/_bot_listener/watchlist_commands.py#L1)).

#### 7. Telemetry and metrics

- Bot Intel shows indicators for the selected target; All Telemetry shows them globally. Both permit text filtering, indicator-type filtering, and copying an indicator value ([frontend/components/TelemetryAnalyticsView.tsx](../frontend/components/TelemetryAnalyticsView.tsx#L40), [frontend/components/TelemetryAnalyticsView.tsx](../frontend/components/TelemetryAnalyticsView.tsx#L205)).
- Dashboard charts let the operator select a time range and show daily capture/message trends, sources, bot health, and takeover outcomes. They read four pre-aggregated Supabase data sets concurrently and show a retryable error if any fails ([frontend/components/DashboardView.tsx](../frontend/components/DashboardView.tsx#L280), [frontend/components/DashboardView.tsx](../frontend/components/DashboardView.tsx#L450)).
- Metric freshness depends on scheduled refresh rather than user-initiated recomputation ([app/workers/celery_app.py](../app/workers/celery_app.py#L315)).

#### 8. Operations controls

- The Ops screen polls system status every 30 seconds while visible, shows Redis status, pause state, pending broadcasts, queue lengths, and bot count, and offers refresh ([frontend/components/OpsPanel.tsx](../frontend/components/OpsPanel.tsx#L35)).
- Pausing requires a confirmation. The interface says in-flight tasks finish and new work waits; the action optimistically updates then rolls back on an unsuccessful proxy response ([frontend/components/OpsPanel.tsx](../frontend/components/OpsPanel.tsx#L126), [frontend/components/OpsPanel.tsx](../frontend/components/OpsPanel.tsx#L331)).
- The dashboard cannot call monitor operations directly: its server proxy verifies the Supabase operator claim, then adds the server-held monitor key. The backend pause state is a Redis key; an unavailable Redis returns 503 and does not claim success ([frontend/app/api/ops/[action]/route.ts](../frontend/app/api/ops/%5Baction%5D/route.ts#L110), [app/api/routers/_monitor/ops.py](../app/api/routers/_monitor/ops.py#L192)).
- Manual scans, paste ingestion, retrying broadcasts, closing revoked topics, health circuit-breaker actions, and detailed queue diagnostics are API/CLI-only, not present in this screen.

#### 9. Telegram control commands and account onboarding

- The bot listener offers `/start`, opt-out aliases, `/status`, `/pause`, `/resume`, `/restart`, `/help`, `/bots`, `/telemetry`, `/getfile`, `/backfill`, and `/watchlist`; command-specific authorization lives in the handlers ([app/services/_bot_listener/runtime.py](../app/services/_bot_listener/runtime.py#L230)).
- `/starthunter` is a private-chat, multi-step Telethon login conversation for adding a user-agent session. By default it is limited to permitted administrators, rate-limited per user, and can only be opened publicly through an explicit risky configuration switch ([app/core/config.py](../app/core/config.py#L38), [app/core/config.py](../app/core/config.py#L151), [app/services/_bot_listener/runtime.py](../app/services/_bot_listener/runtime.py#L252)).
- The documented administrator experience is not identical to the dashboard: the bot gives command-based access to operational actions and account/session administration that the UI does not expose ([README.md](../README.md#L302)).

#### 10. Broadcasts and alerts

- Raw captured-message broadcast is a separate optional behavior. The every-minute broadcast task exits without delivery unless `ENABLE_RAW_MESSAGE_BROADCAST` is true ([app/core/config.py](../app/core/config.py#L121), [app/workers/tasks/flows/broadcast.py](../app/workers/tasks/flows/broadcast.py#L637)).
- Findings are created on a schedule regardless of raw-message broadcast. Finding alert routing is separately disabled unless `FINDING_ALERTS_ENABLED` is true ([app/workers/celery_app.py](../app/workers/celery_app.py#L399), [app/workers/tasks/flows/findings.py](../app/workers/tasks/flows/findings.py#L130)).
- Watchlist alerts have their own queue and forum-topic resolution path; system logs use another topic path. The behavior of outgoing destinations depends on configured bot tokens, group ID, and optional pinned topic IDs ([app/services/broadcaster_srv.py](../app/services/broadcaster_srv.py#L48), [app/core/config.py](../app/core/config.py#L68)).
- This means “a record exists,” “a finding appears,” “an alert is delivered,” and “the raw message is broadcast” are four distinct states, not one automatic outcome.

#### 11. Honeypot and redirect workflows

- When honeypot mode is enabled and a credential is allowed, Telegram can POST updates to `/receive/{credential_id}`. The keyed status endpoint reports honeypot state separately ([app/api/routers/honeypot.py](../app/api/routers/honeypot.py#L75), [app/api/routers/honeypot.py](../app/api/routers/honeypot.py#L234)).
- Captured updates can create evidence and feed a 30-second redirect sweep. Additional reminder and proactive-outreach tasks are scheduled later ([app/workers/celery_app.py](../app/workers/celery_app.py#L432), [app/workers/celery_app.py](../app/workers/celery_app.py#L447)).
- Actual outbound redirect sending requires both redirect mode and redirect authorization to be true. The source defaults both to true, so this is an especially important intent/operational review area ([app/core/config.py](../app/core/config.py#L43)).
- No dashboard view exists for configuring, approving, inspecting, or manually retrying individual honeypot redirects; the available controls are configuration, APIs, and operations scripts.

#### 12. Health, audit, maintenance, and data retention

- The API exposes public liveness at `/health/`; detailed health, metrics, queues, operational state, circuit breakers, quotas, and bot-pool data require the monitor key ([app/api/routers/health.py](../app/api/routers/health.py#L230)).
- The service applies a Redis-backed default rate limit, uses the monitor key as the rate bucket when it is valid, and falls back to request IP otherwise ([app/api/main.py](../app/api/main.py#L102)).
- Scheduled maintenance refreshes stats, watches queues/health, reconciles topics/evidence, checks API keys, reports source quality/attribution, and runs automated retention cleanup ([app/workers/celery_app.py](../app/workers/celery_app.py#L341), [app/workers/celery_app.py](../app/workers/celery_app.py#L479), [app/workers/celery_app.py](../app/workers/celery_app.py#L515)).
- Some maintenance is observable only through logs, Telegram reports, APIs, or CLI tools. The dashboard only exposes the narrow Ops status/control surface and visual metrics.

### Test setup found

- Python uses pytest, with unit/integration/live/load markers configured in [pyproject.toml](../pyproject.toml#L45); 144 Python test files are present under `tests/`.
- The frontend uses TypeScript checking, design-token validation, and Vitest via `npm test` ([frontend/package.json](../frontend/package.json#L5)); 17 frontend test files are present.
- The extension uses Node's built-in test runner ([extension/package.json](../extension/package.json#L2)); two test files are present.
- Tests were not run during this mapping phase.

### Open red flags and inconsistencies

| ID | Observation from code | Impact | Status |
|---|---|---|---|
| P1-01 | The README calls the default stack four always-on services, but `searxng` has no Compose profile and therefore also starts by default. | Documentation/deployment mental-model mismatch. | Open |
| P1-02 | The README documents `AUTO_ARCHIVE_MEDIA=False`; the settings default is `True`. | A fresh deployment may archive/re-upload media when the operator expects it not to. This is a behavioral configuration mismatch. | Open — needs intent before a fix. |
| P1-03 | `frontend/README.md` describes direct authenticated reads and realtime subscriptions on `exfiltrated_messages`, while the current UI uses `evidence_redacted` and comments that raw realtime access was removed after RLS hardening. | Stale developer documentation can encourage an unavailable/unsafe client integration. | Open |
| P1-04 | The `/ingest/extension/credentials` docstring says the Chrome extension writes directly to Supabase, but extension code posts to that endpoint specifically to encrypt server-side. | Stale implementation documentation; no runtime failure identified. | Open |
| P1-05 | The home page only gates on a session and labels it an “Operator session”; operator enforcement occurs later in RLS and server proxies. A signed-in non-operator can see the dashboard shell and then receive denied/empty data rather than a clear access-denied screen. | Wrong-but-harmless authorization UX, assuming migrations are applied; data actions remain protected. | Open — needs product intent. |
| P1-06 | README counts are stale versus the checked-in tree: it says 25 migrations, 79 task handlers, and 402 tests, while the tree contains 50 migrations, 102 task decorators, and 144 Python test files. File counts do not prove test counts, but they establish that the published inventory needs verification. | Documentation/release-status drift. | Open |
| P1-07 | Several operational APIs have no dashboard control: manual scan trigger, manual token paste, raw media proxy, export/retry/topic/webhook/operator/health endpoints, and outpost lease/result. Some are intentionally API/CLI-oriented, but the UI does not expose them. | Potential reachability gap; intent is unclear. | Open — review as their own API/operator feature if desired. |
| P1-08 | Scheduled scanner comments explicitly leave GitLab and Pastebin scan schedules disabled, while scanner provider modules remain imported. | A provider may exist in code but never run on the schedule. This may be intentional due to upstream constraints. | Open — needs intent. |

### Next review step

Choose one feature flow from the map. The next entry will contain one user-point-of-view story, actual acceptance criteria with source references, edge cases, and a confirmation decision before any code or test change.
