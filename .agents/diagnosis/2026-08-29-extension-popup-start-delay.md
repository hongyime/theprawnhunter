# Diagnosis: Chrome extension popup startup delay

Date: 2026-08-29 | Repo: X:\01 REPOSITORIES\theprawnhunter | Status: CONFIRMED

## Symptom Contract

- Expected: opening the Chrome extension popup should show the start controls immediately.
- Observed: the user reports that starting/opening the extension takes a while before the start page appears.
- Scope: Chrome extension popup/start flow, based on the extension files under `extension/`.
- Onset: not confirmed by runtime reproduction; recent extension changes include API URL prefill and several service-worker recovery fixes.
- Repro: click the extension action to open `extension/ui/popup.html`, then wait for `popup.js` to request state from the background service worker.

## Evidence

1. `extension/ui/popup.html` contains a static usable shell with `Ready` and the Start button already present, so the UI can render without backend data. Source: `extension/ui/popup.html`.
2. `extension/ui/popup.js` opens with two async calls: `chrome.storage.local.get(["supabase_config"])` at lines 12-20 and `chrome.runtime.sendMessage({ action: "GET_STATE" })` at lines 34-38. Source: numbered file read.
3. `extension/background.js` handles `GET_STATE` by returning `serializeState(state)` at lines 82-86. Source: numbered file read.
4. `serializeState` returns the entire state object and converts `seenTokens` to an array at lines 171-173. Source: numbered file read.
5. The state includes `results: []` and `seenTokens: new Set()` at lines 18-33, and storage can retain up to `MAX_STORED_RESULTS = 300` result objects at lines 11-13. Source: numbered file read.
6. The popup only uses `status`, `countriesDone`, `resultsFound`, `resultsValid`, `domainMode`, `countryList.length`, `isRunning`, and `isPaused` in `updateUI` at lines 61-106. It does not need full tokens/results or `seenTokens`. Source: numbered file read.
7. The background service worker does multiple cold-start storage/tab operations at top level: `loadState()` at line 36, active tab restore at lines 40-52, and re-entry/alarm checks at lines 58-79. Source: numbered file read.
8. `loadState()` is asynchronous and does not broadcast after state is loaded at lines 143-169, so a cold `GET_STATE` can respond before stored state hydration completes. Source: numbered file read.

## Root Cause

Popup startup is coupled to a Manifest V3 service-worker cold start and a full-state response. On open, the popup asks the background worker for state, and the worker serializes data the popup does not display. When stored scan data grows, this wastes startup time and can also race with async state hydration.

## Why It Was Not Obvious

The popup HTML itself is small and static, so the delay looks like a UI load problem. The actual cost is in the background boundary: waking the service worker, reading persisted state, checking tabs/alarms, and sending a larger-than-needed state payload.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---:|---|
| Summary payload | Return/broadcast only popup fields by default; keep full results internal. | Low | Do now. |
| Popup local cache | Read `scraper_state` and config directly from popup storage first, then hydrate from worker. | Low | Do now. |
| Lazy worker recovery | Move top-level tab/alarm recovery behind a startup function that runs after state load. | Medium | Useful follow-up if popup is still slow. |
| Split extension bundle | Move validation/upload helpers out of background hot path. | Medium | Later cleanup, not required for current symptom. |

## Verification Plan

1. Run syntax checks on `extension/background.js`, `extension/content.js`, and `extension/ui/popup.js`.
2. Confirm popup no longer requests or receives token/result arrays for normal `GET_STATE` rendering.
3. Confirm the popup can still start, stop, resume, and upload by preserving existing message actions.

## Recurrence Guard

Add a lightweight invariant: popup state messages should contain only display fields unless a caller explicitly requests full result data.
