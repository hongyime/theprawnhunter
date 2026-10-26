# Discovery sources

Every source degrades to "zero results plus a warning" when its key, quota, or
upstream is unavailable. Ingestion narrows; the pipeline keeps running.

## Key-free

| Source | Cadence | Notes |
|---|---|---|
| GitHub code search / gists | 4 h / 6 h | uses `GITHUB_TOKENS` pool (search quota 30/min per token) |
| GitLab blobs | disabled | free tier disables global blob search |
| Grep.app | 6 h | public code search |
| Sourcegraph | 12 h | SSE stream search, no auth |
| Common Crawl | daily | index API |
| Wayback Machine | daily 04:00 UTC | CDX + snapshot fetch, 1.2 s pacing |
| Telegram MTProto search | 12 h | user-agent session |
| Docker Hub | hourly | image manifest ENV sweep |
| npm registry | 6 h (:10) | tarballs resolved via package metadata (search API has no `links.tarball`) |
| PyPI | 6 h (:35) | web search is bot-blocked; discovery goes through Exa (`includeDomains: pypi.org`), sdists via JSON API |
| GitHub Actions CI logs | daily 06:20 UTC | ≤25 repos, latest ≤3 runs, zip-bomb guarded |
| **SearXNG** | 6 h (:55) | self-hosted meta-search (Google CSE + Brave); `docker compose up -d searxng` |
| FOFA (browser) | manual | `scripts/ops/fofa_browser.py` against a logged-in Chrome profile |
| FOFA (API) | 4 h when keyed | requires an API-enabled account with F-coins |
| Google CSE | 12 h when keyed | disabled in beat: engine "entire web" is unavailable to new engines, and the project's keys return 403 |
| Bitbucket | dropped | API is workspace-scoped only; global code search was retired |

## Keyed (optional, all degrade gracefully)

| Key | Used by | Check |
|---|---|---|
| `SHODAN_KEY` | Shodan | credits visible in `--only shodan` output |
| `EXA_API_KEY(_2/_3)` | Exa + PyPI discovery | rotates across keys |
| `NETLAS_API_KEY_1/_2` | Netlas | daily budget, per-key limits |
| `URLSCAN_KEY` | URLScan | 1000/day search quota |
| `POSTMAN_API_KEY` | Postman | workspace search |
| `PUBLICWWW_KEY` | PublicWWW | HTML source search |
| `GITHUB_TOKENS` | GitHub/gists/CI logs | pool; dead tokens can be pruned |
| `GITLAB_TOKEN` | GitLab | bot scope |
| `VIRUSTOTAL_API_KEY` | host reputation (+ alerts) | budget capped, cached 24 h |
| `FOFA_EMAIL` + `FOFA_KEY` | FOFA API | needs API-enabled account |

## Testing keys

```bash
python scripts/ops/check_api_keys.py                 # all configured providers
python scripts/ops/check_api_keys.py --only google   # one provider
```

A daily task (`flow.api_key_health`, 07:15 UTC) posts to the **System Logs**
topic only when a provider transitions between working and failing, and writes
state to Redis under `api_key_health:state:<provider>`.

## Watchlist pattern validation

Never enable a pattern without measuring it against the stored corpus:

```bash
python scripts/ops/validate_watchlist_pattern.py "kumarhane|kumar oyna"
```
