# FOFA browser lane

Two independent browser lanes scrape FOFA for bot tokens:

| Lane | Runs in | Schedule | Own tab |
|---|---|---|---|
| Chrome extension (`extension/`) | the Chrome profile you load it into | `chrome.alarms`, every 4 h while Chrome runs | creates its own window |
| Playwright (`scripts/ops/fofa_browser.py`) | a CDP-enabled Chrome profile | systemd user timer (or Task Scheduler) | creates its own tab |

## Domain split

The extension is the leader. It writes `th_fofa_lane_domain` (`cn`/`en`) into
the shared Chrome profile on every start, resume, domain fallback, and
`both`-mode phase switch. The Playwright lane reads that cookie and uses the
**opposite** domain when both are logged in, so the two lanes never scan the
same domain at once:

- extension CN + Playwright EN, or
- extension EN + Playwright CN.

If the opposite domain is logged out, the Playwright probe falls back to the
extension's domain (both use the working one). If a running page lands on
`/f_login`, the extension switches once to the other domain; if both fail it
pauses and posts to System Logs. `--domain auto|cn|en` overrides the pick.

Both lanes stop before scanning with a dead FOFA session and post the reason to
the Telegram **System Logs** topic. A FOFA login lasts 30 days and the server
does not extend it on activity, so re-login when the alert says the token has
fewer than two days left.

## Extension lane

Load `extension/` unpacked in the Chrome profile that is logged in to FOFA and
reload it after an update (the `cookies` permission is new). Keep that Chrome
running: the `auto_scan` alarm fires every 4 h and opens its own FOFA window.
The popup has an "Auto-scan every 4 hours" toggle and stores the query and
domain mode the alarm uses.

## Prerequisites

- A Chrome profile that is logged in to FOFA, separate from your daily profile:
  ```bash
  google-chrome \
    --user-data-dir="$HOME/.config/google-chrome-cdp" \
    --remote-debugging-port=9222 \
    --disable-background-timer-throttling \
    --disable-renderer-backgrounding
  ```

Keep the lane's Chrome window visible (not minimized). Chrome throttles timers
in hidden pages, which slows the popup/content-script waits.
- `.env` at the repo root containing `MONITOR_API_KEY` and `SYSTEM_LOGS_TOPIC_ID`.
  The topic id pin lets the API post to System Logs without a Telethon session.
- The API reachable at `FOFA_INGEST_BASE` (default `http://127.0.0.1:8011`).

The lane opens and closes its own tab. It never reuses the tab the extension or
a person is driving, and it leaves the browser running.

## Install (Linux, systemd user timer)

```bash
mkdir -p ~/.config/systemd/user
ln -sf "$PWD/scripts/deploy/fofa-browser.service" ~/.config/systemd/user/
ln -sf "$PWD/scripts/deploy/fofa-browser.timer" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fofa-browser.timer
systemctl --user list-timers fofa-browser.timer
```

The timer runs at 02:20, 06:20, 10:20, and so on — offset from the
extension's 4-hour alarm so the two lanes do not start together. `Type=oneshot` means a slow
run finishes before the next tick and a second copy is never started.

Run it once by hand:

```bash
systemctl --user start fofa-browser.service
journalctl --user -u fofa-browser.service -n 80 --no-pager
```

## Windows Task Scheduler

Follow the `scripts/deploy/TelegramHunter_Startup.xml` pattern: a 4-hourly or daily trigger,
`MultipleInstancesPolicy IgnoreNew`, and an `Exec` action that runs:

```
C:\theprawnhunter\scripts\ops\run_fofa_browser.bat --cdp http://127.0.0.1:9222 --queries-file scripts\ops\fofa_queries.txt --fetch-hosts 5
```

The command needs Chrome running with `--remote-debugging-port=9222` in the same
user session. The lane exits `3` when the browser or the session is unavailable.

## Exit codes and notifications

| Exit | Meaning | System Logs message |
|---|---|---|
| 0 | run finished | none |
| 2 | profile path or arguments invalid | none |
| 3 | login wall, `[-700]` account-invalid, missing/expiring cookie, CDP down, or profile locked | yes |

Messages use `dedupe_key` so repeated retries inside six hours do not spam the
topic. `send_log` does not redact, so the lane never includes cookies or tokens
in a notification. Use `--no-notify` for an interactive run.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `profile not found` | pass `--profile` or create the directory |
| `FOFA Chrome profile unavailable` | close the Chrome using that profile, or use `--cdp` |
| `CDP Chrome unreachable` | start Chrome with `--remote-debugging-port=9222` |
| `session wall (login)` | log in to FOFA in the same profile, then rerun |
| `[-700] 账号无效` | the account/session is rejected; log in again |
| `本月免费额度已用尽` / `需要F点` | recharge F-coins or wait for the monthly reset |
| `results-missing` after the wait | the result selector or page shape changed |
| No System Logs message | check `MONITOR_API_KEY`, `SYSTEM_LOGS_TOPIC_ID`, and `/health/` |
