const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const popupPath = path.join(__dirname, "..", "ui", "popup.js");
const popupSource = fs.readFileSync(popupPath, "utf8");

const ELEMENT_IDS = [
  "status", "count-country", "count-total", "count-found", "count-valid",
  "btn-start", "btn-stop", "btn-resume", "btn-upload",
  "input-query", "select-domain-mode", "input-api-url", "input-monitor-key",
  "input-auto-scan",
];

function loadPopup(sendMessage) {
  const elements = Object.fromEntries(
    ELEMENT_IDS.map((id) => [id, {
      value: "",
      innerText: "",
      disabled: false,
      style: {},
      classList: { add() {}, remove() {} },
    }]),
  );
  let onReady = null;
  const context = {
    console: { log() {}, warn() {}, error() {} },
    Promise,
    document: {
      addEventListener(event, handler) {
        if (event === "DOMContentLoaded") onReady = handler;
      },
      getElementById: (id) => elements[id],
    },
    chrome: {
      storage: { local: { get(_keys, callback) { callback({}); }, set() {} } },
      runtime: { sendMessage, onMessage: { addListener() {} } },
    },
  };

  vm.runInNewContext(popupSource, context, { filename: popupPath });
  onReady();
  return elements;
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

test("reports an unreachable background instead of an uncaught rejection", async () => {
  const elements = loadPopup(() => Promise.reject(new Error("Could not establish connection. Receiving end does not exist.")));
  await flush();

  assert.match(elements.status.innerText, /reload the extension/);
  assert.equal(elements["btn-start"].disabled, true);
  assert.equal(elements["btn-upload"].disabled, true);
});

test("renders state from a reachable background", async () => {
  const elements = loadPopup(() => Promise.resolve({ status: "Scanning: US", isRunning: true }));
  await flush();

  assert.equal(elements.status.innerText, "Scanning: US");
  assert.equal(elements["btn-start"].disabled, false);
});
