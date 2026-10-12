/**
 * Automated FOFA scraper - launches Chrome with extension, triggers scan via
 * Puppeteer's MV3 service worker API.
 *
 * Usage:
 *   node scripts/dev/run_fofa_scan.mjs
 *
 * Prerequisites:
 *   npm install puppeteer-core  (in repo root)
 *   Chrome installed at default path
 *   FOFA session cookies in the debug profile (first run: log in manually)
 */

import puppeteer from 'puppeteer-core';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, '../..');

const CHROME_PATH = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const EXTENSION_PATH = 'C:\\ext'; // Junction to repo extension path, avoids space issues.
const USER_DATA_DIR = path.join(process.env.TEMP || 'C:\\Temp', 'chrome_fofa_puppeteer');

async function main() {
    console.log('Launching Chrome with extension...');
    console.log('  Extension:', EXTENSION_PATH);
    console.log('  Profile:', USER_DATA_DIR);

    const browser = await puppeteer.launch({
        executablePath: CHROME_PATH,
        headless: false,
        userDataDir: USER_DATA_DIR,
        args: [
            `--load-extension=${EXTENSION_PATH}`,
            `--disable-extensions-except=${EXTENSION_PATH}`,
            '--no-first-run',
            '--no-default-browser-check',
        ],
        defaultViewport: null,
    });

    console.log('Chrome launched. Waiting for extension service worker...');

    // Wait for the extension's service worker to appear
    const swTarget = await browser.waitForTarget(
        target => target.type() === 'service_worker',
        { timeout: 30000 }
    );
    const worker = await swTarget.worker();
    console.log('Service worker found:', swTarget.url());

    // Navigate to FOFA so the content script loads
    const pages = await browser.pages();
    const page = pages[0] || await browser.newPage();
    await page.goto('https://en.fofa.info/', { waitUntil: 'networkidle2', timeout: 30000 });
    console.log('FOFA page loaded:', page.url());

    // Check if logged in (look for username indicator or results)
    const loggedIn = await page.evaluate(() => {
        return !document.body.textContent.includes('Sign in') ||
               document.body.textContent.includes('Personal Center');
    });
    console.log('Logged in to FOFA:', loggedIn);

    if (!loggedIn) {
        console.log('\nNOT LOGGED IN TO FOFA');
        console.log('Please log in manually in this Chrome window, then re-run the script.');
        console.log('The profile is saved at:', USER_DATA_DIR);
        console.log('Next run will reuse the session cookies.\n');
        // Keep browser open for manual login
        await new Promise(() => {}); // hang forever until user closes
    }

    // Trigger the scan via the service worker
    console.log('\nTriggering scan: body="api.telegram.org/bot", both domains, all countries...');
    await worker.evaluate(() => {
        // globalThis.startScan exposed via our background.js patch
        if (typeof globalThis.startScan === 'function') {
            globalThis.startScan('body="api.telegram.org/bot"', 'en.fofa.info', 'both');
            return 'startScan called';
        }
        // Fallback: dispatch via chrome.runtime.onMessage (internal)
        chrome.runtime.sendMessage({
            action: 'START_SCAN',
            query: 'body="api.telegram.org/bot"',
            domain: 'en.fofa.info',
            domainMode: 'both'
        });
        return 'sendMessage dispatched';
    });

    console.log('Scan triggered! Monitoring progress...\n');

    // Monitor progress every 30s
    const checkInterval = setInterval(async () => {
        try {
            const status = await worker.evaluate(() => {
                const s = globalThis.state || {};
                return JSON.stringify({
                    running: s.isRunning,
                    paused: s.isPaused,
                    status: s.status,
                    country: s.countryIndex,
                    total: (s.countryList || []).length,
                    found: s.resultsFound,
                    valid: s.resultsValid,
                });
            });
            const st = JSON.parse(status);
            const pct = st.total ? Math.round(st.country / st.total * 100) : 0;
            console.log(`[${new Date().toLocaleTimeString()}] ${st.status} | ${pct}% | found:${st.found} valid:${st.valid}`);

            if (!st.running && !st.paused && st.country > 0) {
                console.log('\nScan complete!');
                clearInterval(checkInterval);
                // Give upload time to finish
                await new Promise(r => setTimeout(r, 10000));
                await browser.close();
                process.exit(0);
            }

            if (st.paused) {
                console.log('Scan paused:', st.status);
                console.log('   Attempting auto-resume in 60s...');
                await new Promise(r => setTimeout(r, 60000));
                await worker.evaluate(() => {
                    if (typeof globalThis.resumeScan === 'function') globalThis.resumeScan();
                    else chrome.runtime.sendMessage({ action: 'RESUME_SCAN' });
                });
            }
        } catch (e) {
            console.log('Monitor error (SW may have restarted):', e.message);
        }
    }, 30000);
}

main().catch(e => {
    console.error('Fatal:', e);
    process.exit(1);
});
