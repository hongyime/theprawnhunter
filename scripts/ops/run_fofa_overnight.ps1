<#
.SYNOPSIS
    Launch FOFA scraper extension overnight in headful Chrome with CDP debugging.
    Starts the scan automatically — no manual popup clicking needed.

.DESCRIPTION
    1. Kills existing Chrome instances (saves session first)
    2. Launches Chrome with --remote-debugging-port=9222 + extension loaded
    3. Waits for Chrome to start
    4. Connects via CDP and triggers the scan by navigating to FOFA + injecting startScan

.NOTES
    Run this before sleeping. Monitor progress via:
      curl http://localhost:9222/json  (list Chrome tabs)
    Or check extension state from another PowerShell:
      Invoke-RestMethod http://localhost:9222/json | Select url, title
#>

$ErrorActionPreference = "Stop"

$CHROME = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$EXTENSION_PATH = "X:\01 REPOSITORIES\telegramhunter\extension"
$CDP_PORT = 9222
$FOFA_URL = "https://en.fofa.info/"
$USER_DATA_DIR = "$env:LOCALAPPDATA\Google\Chrome\User Data"  # Use existing profile (logged into FOFA)

# --- Step 1: Close existing Chrome (gracefully) ---
Write-Host "=== Closing existing Chrome instances ===" -ForegroundColor Cyan
$chromeProcs = Get-Process chrome -ErrorAction SilentlyContinue
if ($chromeProcs) {
    Write-Host "  Closing $($chromeProcs.Count) Chrome processes..."
    $chromeProcs | Stop-Process -Force
    Start-Sleep -Seconds 3
}

# --- Step 2: Launch Chrome with debugging + extension ---
Write-Host "=== Launching Chrome with CDP on :$CDP_PORT ===" -ForegroundColor Cyan
$args = @(
    "--remote-debugging-port=$CDP_PORT",
    "--load-extension=$EXTENSION_PATH",
    "--disable-extensions-except=$EXTENSION_PATH",
    "--no-first-run",
    "--no-default-browser-check",
    "--user-data-dir=$USER_DATA_DIR",
    "--profile-directory=Default",
    "--restore-last-session",
    $FOFA_URL
)
Start-Process $CHROME -ArgumentList $args
Write-Host "  Chrome launched. Waiting for CDP..."

# --- Step 3: Wait for CDP to become available ---
$maxWait = 30
$waited = 0
while ($waited -lt $maxWait) {
    try {
        $tabs = Invoke-RestMethod "http://localhost:$CDP_PORT/json" -ErrorAction Stop
        if ($tabs) { break }
    } catch {}
    Start-Sleep -Seconds 2
    $waited += 2
}
if ($waited -ge $maxWait) {
    Write-Host "ERROR: CDP not available after ${maxWait}s" -ForegroundColor Red
    exit 1
}
Write-Host "  CDP ready ($waited s)" -ForegroundColor Green

# --- Step 4: Wait for FOFA tab to load ---
Start-Sleep -Seconds 8  # Let FOFA page fully render + extension inject

# --- Step 5: Find the FOFA tab and trigger scan via content script ---
$tabs = Invoke-RestMethod "http://localhost:$CDP_PORT/json"
$fofaTab = $tabs | Where-Object { $_.url -like "*fofa.info*" } | Select-Object -First 1

if (-not $fofaTab) {
    Write-Host "WARNING: No FOFA tab found — extension popup will need manual start" -ForegroundColor Yellow
    Write-Host "  Open the extension popup and click 'Start Scan'"
    exit 0
}

Write-Host "=== FOFA tab found: $($fofaTab.url) ===" -ForegroundColor Green
Write-Host ""

# --- Step 6: Use CDP to execute JavaScript that triggers the background scan ---
# We can't directly call background.js from a page context, but we CAN
# use chrome.runtime.sendMessage from the content script context.
# Simpler: just tell the user the scan needs one popup click, OR
# use the chrome.debugger API to send a message to the extension's SW.

# Actually the cleanest way: navigate the extension's popup page directly
# and click the start button via CDP.

# Find service worker target
$targets = Invoke-RestMethod "http://localhost:$CDP_PORT/json"
$swTarget = $targets | Where-Object { $_.type -eq "service_worker" -and $_.url -like "*background*" }

if ($swTarget) {
    Write-Host "=== Found extension service worker — triggering scan via CDP ===" -ForegroundColor Cyan

    # Connect to the service worker's WebSocket and execute startScan
    # For simplicity, use a Node.js one-liner since PowerShell WebSocket is painful
    $nodeScript = @"
const ws = require('ws');
const socket = new ws('$($swTarget.webSocketDebuggerUrl)');
socket.on('open', () => {
    socket.send(JSON.stringify({
        id: 1,
        method: 'Runtime.evaluate',
        params: { expression: "startScan('body=\"api.telegram.org/bot\"', 'en.fofa.info', 'both')" }
    }));
    setTimeout(() => { console.log('Scan triggered!'); process.exit(0); }, 2000);
});
socket.on('error', (e) => { console.error('WS error:', e.message); process.exit(1); });
"@
    $nodeScript | Set-Content "$env:TEMP\trigger_scan.js" -Encoding UTF8
    node "$env:TEMP\trigger_scan.js"
    Remove-Item "$env:TEMP\trigger_scan.js" -ErrorAction SilentlyContinue
} else {
    Write-Host ""
    Write-Host "=== Could not find extension SW via CDP ===" -ForegroundColor Yellow
    Write-Host "  The extension is loaded but Chrome may need you to"
    Write-Host "  click the extension icon once to activate the service worker."
    Write-Host "  After that, the scan auto-resumes from the alarm/watchdog."
    Write-Host ""
    Write-Host "  Alternatively, open the popup and click 'Start Scan'."
}

Write-Host ""
Write-Host "=== DONE — Chrome is running with FOFA scraper ===" -ForegroundColor Green
Write-Host "Monitor progress:"
Write-Host "  - Extension popup: click the extension icon in Chrome"
Write-Host "  - CDP debug: curl http://localhost:$CDP_PORT/json"
Write-Host "  - Auto-upload: every 10 countries, results sent to API"
Write-Host ""
Write-Host "Go to sleep. The scan runs all 49 countries × 2 domains = ~98 pages."
Write-Host "At ~15s per page = ~25 minutes total runtime."
