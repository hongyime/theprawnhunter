
// CDP automation hook: scripts/run_fofa_overnight.ps1 sends postMessage
// from the page context. Content script bridges it to the background SW.
window.addEventListener("message", (event) => {
    if (event.data && event.data.type === "TH_START_SCAN") {
        chrome.runtime.sendMessage({
            action: "START_SCAN",
            query: event.data.query || 'body="api.telegram.org/bot"',
            domain: event.data.domain || "en.fofa.info",
            domainMode: event.data.mode || "both",
        });
        console.log("[TH Content] CDP automation: START_SCAN forwarded to background");
    }
});

// --- SELECTORS ---
// Multiple fallback selectors for each FOFA UI element.
// FOFA updates its CSS class names periodically — fallbacks keep the extension
// working when the primary selector breaks without any code change needed.

const CLICK_SELECTORS = [
    'i.iconfont.icon-daima',          // primary: "view source" icon (current FOFA)
    'i.iconfont.icon-code',           // alt class seen in some regions
    '.hsxa-host-table-body-item .iconfont',  // broader table-item icon match
    '[title="Source code"]',          // attribute-based fallback
    '[class*="icon-daima"]',          // partial class match
    '[class*="icon-code"]',
    'i[class*="icon-"]',              // generic icon fallback
];

const POPUP_CONTENT_SELECTORS = [
    '.source-content',                // primary: HTML source viewer
    '.el-dialog__body pre',           // code in pre block
    '.el-dialog__body code',
    '.el-dialog__body .CodeMirror-code',  // CodeMirror editor
    '.el-dialog__body',               // whole dialog body as last resort
];

const RESULT_ROW_SELECTORS = [
    '.hsxa-host-table-body .hsxa-host-table-body-item',
    '.host-table-body .host-table-body-item',
    '[class*="result-item"]',
    '[class*="host-item"]',
];

// Token regex: 5-15 digit bot_id + colon + 20-50 char secret.
// Broadened from 8-13:35 to catch edge cases (short bot IDs from early bots,
// longer secrets from newer Telegram API versions).
// DATA-005: align to canonical Telegram bot token shape used across
// the backend (app/utils/helpers.py and app/services/scanners.py).
// Bot IDs are always 8-15 digits; secret is exactly 35 URL-safe chars.
const TOKEN_REGEX = /['"` ]?(\d{8,15}:[A-Za-z0-9_-]{35})['"` \n\r]?/g;

// --- STATE ---
let isWorking  = false;
let shouldStop = false;

// --- LISTENERS ---
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === "SCRAPE_PAGE") {
        shouldStop = false;
        if (isWorking) return;
        startScraping().catch(e => log(`Critical error: ${e.message}`));
    } else if (msg.action === "RESUME_WORK") {
        shouldStop = false;
        startScraping().catch(e => log(`Critical error: ${e.message}`));
    } else if (msg.action === "STOP_WORK") {
        shouldStop  = true;
        isWorking   = false;
        log("Stopped by user.");
    }
});

// --- CORE ---
async function startScraping() {
    isWorking = true;
    try {
        log("Starting scraping on this page...");

        // 1. Captcha check
        if (checkForCaptcha()) {
            chrome.runtime.sendMessage({ action: "CAPTCHA_DETECTED" });
            return;
        }

        // 2. Login-wall check (FOFA -3000: IP flagged as crawler, login required)
        if (checkLoginWall()) {
            chrome.runtime.sendMessage({ action: "LOGIN_REQUIRED" });
            return;
        }

        // Collect all tokens found on this page (dedup by token string)
        const pageTokenMap = new Map(); // token -> {token, chatId}

        // 2. Fast pass: scan full page text + all visible URLs for tokens
        // textContent is better than innerText for catching script tags/hidden meta
        const fastTokens = extractTokensFromText(document.body.textContent);
        
        // Also scan all href/src attributes — tokens sometimes appear in URLs
        document.querySelectorAll("a[href], script[src]").forEach((el) => {
            const url = el.href || el.src || "";
            extractTokensFromText(url).forEach((t) => fastTokens.push(t));
        });

        const uniqueFast = [...new Set(fastTokens)];
        if (uniqueFast.length > 0) {
            log(`Fast pass: ${uniqueFast.length} token(s) in page text/URLs`);
            uniqueFast.forEach((token) => {
                if (!pageTokenMap.has(token)) pageTokenMap.set(token, { token, chatId: "" });
            });
        }

        // 3. Click pass: open each result's source popup for deep extraction
        const items = getVisibleItems();

        if (items.length === 0) {
            // Distinguish "no results for this country" from "selector broke"
            const hasResultContainer = !!document.querySelector(
                RESULT_ROW_SELECTORS.map(s => s.split(' ')[0]).join(',')
            );
            if (hasResultContainer) {
                log("Result container found but no clickable icons — FOFA selector may have changed");
            } else {
                log("No results for this country — moving on");
            }
        } else if (!shouldStop) {
            log(`Click pass: ${items.length} item(s)...`);

            for (let i = 0; i < items.length; i++) {
                if (shouldStop) { log("Aborted mid-page."); break; }

                const el = items[i];
                el.style.border = "3px solid #f0f";
                el.scrollIntoView({ behavior: "smooth", block: "center" });
                el.click();

                const content = await waitForPopupContent(8000);

                if (content) {
                    const { tokens, chatId, allChatIds } = extractData(content);
                    log(`Popup: ${tokens.length} token(s), chatId=${chatId || "none"}`);
                    tokens.forEach((token, idx) => {
                        // Use the chatId from the same popup; for multi-token popups
                        // try to pair each token with the nearest chat_id match
                        const cid = allChatIds[idx] || chatId || "";
                        if (!pageTokenMap.has(token)) {
                            pageTokenMap.set(token, { token, chatId: cid });
                        }
                    });
                } else {
                    log("Popup timed out — skipping item");
                }

                closePopup();
                el.style.border = "";

                if (shouldStop) break;
                await delay(800); // XHR takes ~700-900ms; give dialog time to fully close
            }
        }

        // Send all found tokens as a single batch to background.js
        const batch = Array.from(pageTokenMap.values());
        if (batch.length > 0) {
            log(`Sending batch of ${batch.length} token(s) to background`);
            chrome.runtime.sendMessage({ action: "RESULTS_FOUND", data: batch });
        }

        log("Page complete.");
        if (!shouldStop) {
            chrome.runtime.sendMessage({ action: "PAGE_COMPLETE" });
        }
    } finally {
        isWorking = false;
    }
}

// --- HELPERS ---

function log(msg) {
    console.log("[TH Content]", msg);
    chrome.runtime.sendMessage({ action: "LOG", message: msg });
}

function delay(ms) {
    return new Promise((r) => setTimeout(r, ms));
}

function checkForCaptcha() {
    const text = document.body.textContent;
    return (
        text.includes("Human-machine verification") ||
        text.includes("Slide to complete puzzle") ||
        text.includes("请完成安全验证") ||
        !!document.querySelector(".verify-wrap, #captcha, .nc-container")
    );
}

// Detect FOFA's IP-ban / login-required wall (-3000 error).
// Returns true if the page is showing the login prompt instead of results.
// Strings cover both the EN and CN FOFA domains.
function checkLoginWall() {
    const text = document.body.textContent;
    return (
        text.includes("-3000")                                         ||
        text.includes("IP access is abnormal")                        ||
        text.includes("suspected to be a web crawler")                ||
        text.includes("can be used after logging in")                 ||
        text.includes("IP访问异常")                                    ||
        text.includes("疑似为爬虫")                                    ||
        text.includes("登录账号可用")                                  ||
        // FOFA login page itself (redirected away from results)
        !!document.querySelector(
            ".login-form, #login-form, [class*='login-container'], [action*='/login']"
        )
    );
}

function getVisibleItems() {
    for (const selector of CLICK_SELECTORS) {
        const all = document.querySelectorAll(selector);
        const visible = Array.from(all).filter((el) => el.offsetParent !== null);
        if (visible.length > 0) {
            log(`Using selector: ${selector} (${visible.length} items)`);
            return visible;
        }
    }
    return [];
}

async function waitForPopupContent(timeout) {
    let elapsed = 0;
    let dialogSeenAt = null;

    while (elapsed < timeout) {
        // Try each popup content selector in order
        for (const sel of POPUP_CONTENT_SELECTORS) {
            const el = document.querySelector(sel);
            if (el && el.textContent && el.textContent.length > 30) {
                return decodeHTMLEntities(el.textContent);
            }
        }

        // Track when the dialog first appeared (XHR has been dispatched)
        const dialogOpen = document.querySelector(
            '.el-overlay.el-modal-dialog, .el-dialog__wrapper:not([style*="display: none"])'
        );
        if (dialogOpen && dialogOpen.textContent.trim().length > 10) {
            if (!dialogSeenAt) dialogSeenAt = elapsed;
            // Dialog has been open for 2.5s but source-content still empty?
            // This host has no indexed body content — bail early, don't waste 8s.
            if (elapsed - dialogSeenAt > 2500) {
                log("Dialog open but no source content after 2.5s — empty host, skipping");
                // Close the empty dialog before returning
                const cb = document.querySelector('.el-dialog__headerbtn, [aria-label="Close"]');
                if (cb) cb.click();
                else document.body.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape',keyCode:27,bubbles:true}));
                return null;
            }
        }

        // Try iframes inside the dialog
        const iframes = document.querySelectorAll(".el-dialog__body iframe");
        for (const iframe of iframes) {
            try {
                const doc = iframe.contentDocument || iframe.contentWindow.document;
                if (doc && doc.body && doc.body.textContent.length > 30) {
                    return doc.body.textContent;
                }
            } catch (_) { /* cross-origin */ }
        }

        await delay(300);
        elapsed += 300;
    }
    return null;
}

function decodeHTMLEntities(text) {
    const ta = document.createElement("textarea");
    ta.innerHTML = text;
    return ta.value;
}

function extractTokensFromText(text) {
    const found = [];
    let m;
    const re = new RegExp(TOKEN_REGEX.source, "g");
    while ((m = re.exec(text)) !== null) {
        found.push(m[1]);
    }
    return found;
}

function extractData(rawText) {
    // Extract ALL tokens from the popup source (not just the first)
    const tokens = extractTokensFromText(rawText);

    // Chat ID patterns — try to find one per token (positional match)
    // Covers: CHAT_ID = '...', chat_id: ..., chatId=..., target: -100..., cid=...
    const chatIdRegex = /(?:CHAT_ID|chat_id|chatId|chat|target|cid)\s*[=:]\s*['"]?(-?\d{5,20})['"]?/gi;
    const allChatIds  = [];
    let cm;
    while ((cm = chatIdRegex.exec(rawText)) !== null) {
        allChatIds.push(cm[1]);
    }
    const chatId = allChatIds[0] || "";

    console.log("[TH Extract] tokens:", tokens, "chatIds:", allChatIds);

    return { tokens, chatId, allChatIds };
}

function closePopup() {
    // Escape key
    document.body.dispatchEvent(
        new KeyboardEvent("keydown", {
            key: "Escape", code: "Escape", keyCode: 27, which: 27, bubbles: true,
        })
    );
    // Close button
    const closeBtn = document.querySelector(".el-dialog__headerbtn, [aria-label='Close'], .dialog-close");
    if (closeBtn) closeBtn.click();
}
