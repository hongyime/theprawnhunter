document.addEventListener("DOMContentLoaded", () => {
    const DEFAULT_API_URL = "https://winnethepooh.hong-yi.me";
    const DEFAULT_COUNTRY_TOTAL = 49;

    const btnStart      = document.getElementById("btn-start");
    const btnStop       = document.getElementById("btn-stop");
    const btnResume     = document.getElementById("btn-resume");
    const btnUpload     = document.getElementById("btn-upload");
    const inputQuery    = document.getElementById("input-query");
    const selectMode    = document.getElementById("select-domain-mode");
    const inputApiUrl   = document.getElementById("input-api-url");
    const inputMonKey   = document.getElementById("input-monitor-key");
    const elTotal       = document.getElementById("count-total");

    // Render from local storage first so popup paint does not depend on
    // waking the Manifest V3 background service worker.
    chrome.storage.local.get(["supabase_config", "scraper_state"], (result) => {
        const cfg = result.supabase_config || {};
        inputApiUrl.value = cfg.apiUrl || DEFAULT_API_URL;
        if (cfg.monitorKey) inputMonKey.value = cfg.monitorKey;
        if (result.scraper_state) updateUI(summarizeStoredState(result.scraper_state));
        if (!cfg.apiUrl) saveConfig();
    });

    function saveConfig() {
        chrome.storage.local.set({
            supabase_config: {
                apiUrl:     (inputApiUrl.value || "").trim(),
                monitorKey: (inputMonKey.value || "").trim(),
            }
        });
    }

    inputApiUrl.onchange = saveConfig;
    inputMonKey.onchange = saveConfig;

    // Refresh from the background worker when it is awake.
    chrome.runtime.sendMessage({ action: "GET_STATE" }, (response) => {
        if (chrome.runtime.lastError) return;
        updateUI(response);
    });

    // Live updates while popup is open
    chrome.runtime.onMessage.addListener((msg) => {
        if (msg.action === "STATE_UPDATE") updateUI(msg.state);
    });

    btnStart.onclick = () => {
        saveConfig();
        chrome.runtime.sendMessage({
            action:     "START_SCAN",
            query:      inputQuery.value,
            domainMode: selectMode.value,
        });
    };

    btnStop.onclick   = () => chrome.runtime.sendMessage({ action: "STOP_SCAN" });
    btnResume.onclick = () => chrome.runtime.sendMessage({ action: "RESUME_SCAN" });
    btnUpload.onclick = () => {
        saveConfig();
        chrome.runtime.sendMessage({ action: "UPLOAD_RESULTS" });
    };

    function summarizeStoredState(state) {
        const countryList = Array.isArray(state.countryList) ? state.countryList : [];
        return {
            isRunning: !!state.isRunning,
            isPaused: !!state.isPaused,
            status: state.status || "Ready",
            query: state.query,
            domainMode: state.domainMode || "en",
            countriesDone: state.countriesDone || 0,
            resultsFound: state.resultsFound || 0,
            resultsValid: state.resultsValid || 0,
            countryTotal: state.domainMode === "both"
                ? (countryList.length || DEFAULT_COUNTRY_TOTAL) * 2
                : (countryList.length || DEFAULT_COUNTRY_TOTAL),
        };
    }

    function updateUI(state) {
        if (!state) return;

        document.getElementById("status").innerText        = state.status;
        document.getElementById("count-country").innerText = state.countriesDone || 0;
        document.getElementById("count-found").innerText   = state.resultsFound  || 0;
        const validEl = document.getElementById("count-valid");
        if (validEl) validEl.innerText = state.resultsValid || 0;
        if (!state.isRunning && state.domainMode) selectMode.value = state.domainMode;
        // Both mode scans the current country set across two domains.
        if (elTotal) {
            elTotal.innerText = state.countryTotal || (
                state.domainMode === "both" ? DEFAULT_COUNTRY_TOTAL * 2 : DEFAULT_COUNTRY_TOTAL
            );
        }

        if (state.isRunning && !state.isPaused) {
            btnStart.classList.add("hidden");
            btnStop.classList.remove("hidden");
            btnResume.classList.add("hidden");
            inputQuery.disabled   = true;
            selectMode.disabled   = true;
            document.getElementById("status").style.color = "#fb0";
        } else if (state.isPaused) {
            btnStop.classList.add("hidden");
            btnResume.classList.remove("hidden");
            btnStart.classList.add("hidden");
            const statusEl = document.getElementById("status");
            // Show the actual pause reason from background (login wall vs captcha)
            statusEl.innerText = state.status;
            statusEl.style.color = "red";
        } else {
            btnStop.classList.add("hidden");
            btnResume.classList.add("hidden");
            btnStart.classList.remove("hidden");
            inputQuery.disabled   = false;
            selectMode.disabled   = false;
            document.getElementById("status").style.color = "#fb0";

            if (state.resultsFound > 0) {
                btnStart.innerText             = "🔄 New Scan (clears data)";
                btnStart.style.backgroundColor = "#ff9800";
            } else {
                btnStart.innerText             = "🚀 Start";
                btnStart.style.backgroundColor = "";
            }
        }

        // Upload button: active when there are ANY found results and not currently running
        // Backend validates tokens itself — send everything found, not just pre-validated ones
        btnUpload.disabled = !(state.resultsFound > 0 && !state.isRunning);
    }
});
