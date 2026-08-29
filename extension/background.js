// --- CONFIGURATION ---
const BASE_QUERY_TEMPLATE = 'body="api.telegram.org/bot"';
const COUNTRY_CODES = [
    "US", "CN", "HK", "RU", "FR", "DE", "NL", "SG", "GB", "JP",
    "KR", "IN", "BR", "CA", "AU", "IT", "ES", "TR", "UA", "VN",
    "ID", "PL", "SE", "CH", "NO", "FI", "DK", "IE", "AT", "CZ",
    "RO", "ZA", "MX", "AR", "CO", "CL", "MY", "TH", "PH", "PK",
    "IR", "SA", "AE", "IL", "GR", "PT", "BE", "HU", "NZ"
];

// Max tokens kept in state.results to avoid hitting the 5 MB
// chrome.storage.local limit. Oldest entries are dropped when exceeded.
const MAX_STORED_RESULTS = 300;

// Max concurrent getMe validation calls (keeps Telegram rate-limit happy)
const VALIDATE_CONCURRENCY = 5;

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
                if (chrome.runtime.lastError || !tab) {
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
                                .catch(() => nextCountry());
                        }
                        // If tab is still loading, onUpdated will fire and create the alarm
                    });
                }, 1000);
            }
        });
    });
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
            pauseScan("🔒 Login required — log into FOFA then click Resume");
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
            // Storage full — trim results and retry once
            if (state.results.length > 50) {
                state.results = state.results.slice(-50);
                const trimmed = { ...storageState, results: state.results };
                chrome.storage.local.set({ scraper_state: trimmed }, () => {
                    if (chrome.runtime.lastError) {
                        console.error("[BG] saveState retry failed — state not persisted");
                    }
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
                // Mark as stopped if it was mid-run when the SW died
                if (loaded.isRunning && !loaded.isPaused) {
                    loaded.isRunning = false;
                    loaded.status = "Stopped (Recovered)";
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

    saveState();

    // Find the FOFA tab — prefer active tab if it's FOFA, otherwise find any FOFA tab
    let tab = null;
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (activeTab && (activeTab.url || "").includes("fofa.info")) {
        tab = activeTab;
    } else {
        const fofaTabs = await chrome.tabs.query({ url: ["https://fofa.info/*", "https://en.fofa.info/*"] });
        tab = fofaTabs[0] || activeTab; // fall back to active tab if no FOFA tab open
    }
    if (!tab) { stopScan("No active tab found"); return; }
    activeTabId = tab.id;
    chrome.storage.local.set({ activeTabId: tab.id });

    chrome.alarms.create("watchdog", { periodInMinutes: 3 });

    broadcastState();
    processNextCountry();
}

function stopScan(reason) {
    state.isRunning = false;
    state.isPaused  = false;
    state.status    = reason || "Stopped";
    chrome.alarms.clearAll();
    chrome.storage.local.remove("activeTabId");
    if (activeTabId) {
        chrome.tabs.sendMessage(activeTabId, { action: "STOP_WORK" }).catch(() => {});
    }
    activeTabId = null;
    saveState();
    broadcastState();
}

function pauseScan(reason) {
    state.isPaused = true;
    state.status   = reason || "Paused";
    saveState();
    broadcastState();
}

function resumeScan() {
    if (!state.isRunning && state.status !== "Stopped (Recovered)") return;
    if (!state.isRunning) state.isRunning = true;
    state.isPaused = false;
    state.status   = "Resuming...";
    saveState();
    broadcastState();

    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
        if (tab) {
            activeTabId = tab.id;
            chrome.storage.local.set({ activeTabId: tab.id });
            chrome.tabs.sendMessage(activeTabId, { action: "RESUME_WORK" }).catch(() => {
                processNextCountry(false);
            });
        } else {
            stopScan("Could not find active tab to resume");
        }
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

    // Navigate the tracked tab — if it's dead, find a FOFA tab or bail clearly
    try {
        await chrome.tabs.update(activeTabId, { url: targetUrl });
    } catch (e) {
        // Tab is gone — try to find any open FOFA tab to take over
        const fofaTabs = await chrome.tabs.query({ url: ['https://fofa.info/*','https://en.fofa.info/*'] });
        if (fofaTabs.length) {
            activeTabId = fofaTabs[0].id;
            chrome.storage.local.set({ activeTabId });
            await chrome.tabs.update(activeTabId, { url: targetUrl });
        } else {
            pauseScan('⚠️ FOFA tab was closed — reopen FOFA then click Resume');
            return;
        }
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
            nextCountry();
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
                        nextCountry();
                    });
                }
            });
        }
    }
});

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
async function handleResults(items) {
    if (!state.isRunning && state.status !== "Paused") return;

    // Dedup against already-seen tokens
    const newItems = items.filter((d) => {
        if (!d.token || state.seenTokens.has(d.token)) return false;
        state.seenTokens.add(d.token);
        return true;
    });

    if (newItems.length === 0) return;

    // Save raw tokens immediately before validation — survives SW death mid-batch
    for (const item of newItems) {
        if (state.results.length >= MAX_STORED_RESULTS) state.results.shift();
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
            // Fallback: shouldn't happen but cap and push anyway
            if (state.results.length >= MAX_STORED_RESULTS) state.results.shift();
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

async function uploadToSupabase() {
    // Send ALL found results — valid, invalid, and unvalidated
    // The backend re-validates via enrich_credential anyway
    const allResults = (state.results || []);
    if (allResults.length === 0) {
        state.status = "⚠️ Nothing to upload (no tokens found)";
        saveState();
        broadcastState();
        return;
    }

    const cfg = await new Promise((resolve) => {
        chrome.storage.local.get(["supabase_config"], (r) => resolve(r.supabase_config || {}));
    });

    const apiUrl          = (cfg.apiUrl          || "").trim().replace(/\/+$/, "");
    const monitorKey      = (cfg.monitorKey       || "").trim();

    if (!apiUrl) {
        state.status = "⚠️ Upload skipped — set API URL in settings";
        saveState();
        broadcastState();
        return;
    }

    state.status = `⬆️ Uploading ${allResults.length} tokens via API...`;
    saveState();
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

    saveState();
    broadcastState();
}

// Expose for CDP automation (scripts/run_fofa_scan.mjs)
globalThis.startScan = startScan;
globalThis.stopScan = stopScan;
globalThis.resumeScan = resumeScan;
globalThis.state = state;

