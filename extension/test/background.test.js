const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const backgroundPath = path.join(__dirname, "..", "background.js");
const backgroundSource = fs.readFileSync(backgroundPath, "utf8");

function loadBackground(responses) {
  const requests = [];
  const stored = {
    supabase_config: { apiUrl: "https://api.example.test", monitorKey: "synthetic-key" },
  };
  const noListener = { addListener() {} };
  const chrome = {
    runtime: {
      onMessage: noListener,
      onInstalled: noListener,
      onStartup: noListener,
      sendMessage: () => Promise.resolve(),
    },
    storage: {
      local: {
        get(keys, callback) {
          callback(Object.fromEntries(keys.map((key) => [key, stored[key]])));
        },
      },
    },
    tabs: {
      onUpdated: noListener,
      get(_tabId, callback) {
        callback(null);
      },
    },
    alarms: {
      onAlarm: noListener,
      create() {},
      clear() {},
      get(_name, callback) {
        callback(null);
      },
    },
  };
  const context = {
    chrome,
    AbortController,
    URL,
    setTimeout,
    clearTimeout,
    console: { log() {}, warn() {}, error() {} },
    fetch: async (url, options) => {
      requests.push({ url, ...options });
      const reply = responses.shift();
      return {
        ok: reply.status >= 200 && reply.status < 300,
        status: reply.status,
        json: async () => reply.body || {},
        text: async () => reply.text || "test response",
      };
    },
  };

  vm.runInNewContext(
    `${backgroundSource}\nglobalThis.__testHooks = {\n` +
      "  get state() { return state; }, stateReady, makeRoomForResults, sessionIssue\n" +
      "};",
    context,
    { filename: backgroundPath },
  );

  return { hooks: context.__testHooks, requests };
}

function makeResults(count) {
  return Array.from({ length: count }, (_, index) => ({
    token: `synthetic-token-${index}`,
    valid: true,
  }));
}

test("keeps results when the upload fails while making room", async () => {
  const { hooks, requests } = loadBackground([{ status: 400 }]);
  await hooks.stateReady;
  const original = makeResults(300);
  hooks.state.results = original;

  await hooks.makeRoomForResults(1);

  assert.deepEqual(hooks.state.results.map((result) => result.token), original.map((result) => result.token));
  assert.equal(requests.length, 1);
  assert.equal(JSON.parse(requests[0].body).results.length, 300);
});

test("prunes the oldest results only after the upload succeeds", async () => {
  const { hooks, requests } = loadBackground([
    { status: 200, body: { inserted: 300 } },
  ]);
  await hooks.stateReady;
  hooks.state.results = makeResults(300);

  await hooks.makeRoomForResults(1);

  assert.equal(JSON.parse(requests[0].body).results.length, 300);
  assert.equal(hooks.state.results.length, 299);
  assert.equal(hooks.state.results[0].token, "synthetic-token-1");
  assert.equal(hooks.state.results.at(-1).token, "synthetic-token-299");
});

test("warns only inside the final week of the FOFA session", () => {
  const { hooks } = loadBackground([]);
  const now = Date.UTC(2026, 9, 30, 12, 0, 0);
  const inDays = (days) => ({ expirationDate: now / 1000 + days * 86400 });

  assert.equal(hooks.sessionIssue(inDays(30), now), null);
  assert.equal(hooks.sessionIssue(inDays(3), now), null);
  assert.match(hooks.sessionIssue(inDays(1), now), /expires in 1 day/);
  assert.match(hooks.sessionIssue(inDays(0), now), /expired/);
  assert.match(hooks.sessionIssue(null, now), /not found/);
});
