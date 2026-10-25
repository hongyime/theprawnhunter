// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  GET,
  OPS_NOT_CONFIGURED_REASON,
  POST,
} from "./route";

const savedEnv = {
  apiBase: process.env.API_BASE_URL,
  monitorKey: process.env.MONITOR_API_KEY,
};

function restore(key: "API_BASE_URL" | "MONITOR_API_KEY", value?: string) {
  if (value === undefined) {
    delete process.env[key];
  } else {
    process.env[key] = value;
  }
}

function context(action: string) {
  return { params: Promise.resolve({ action }) };
}

async function readJson(response: Response) {
  return JSON.parse(await response.text());
}

describe("/api/ops/[action]", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    process.env.API_BASE_URL = "http://api.test/";
    process.env.MONITOR_API_KEY = "server-only-secret";
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    restore("API_BASE_URL", savedEnv.apiBase);
    restore("MONITOR_API_KEY", savedEnv.monitorKey);
  });

  it("proxies GET status to the upstream status path with the key", async () => {
    const payload = {
      paused: false,
      redis: "ok",
      pending_broadcasts: 12,
      queue_lengths: {
        celery: 1,
        scrape: 2,
        scanners: 3,
        validation: 4,
      },
      bot_count: 2,
    };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await GET(
      new Request("http://dashboard.test/api/ops/status"),
      context("status"),
    );

    expect(response.status).toBe(200);
    expect(await readJson(response)).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/monitor/ops/status");
    expect(init.method).toBe("GET");
    expect(init.headers["X-Monitor-Key"]).toBe("server-only-secret");
  });

  it.each(["pause", "resume"] as const)(
    "proxies POST %s to the matching upstream path",
    async (action) => {
      fetchMock.mockResolvedValue(
        new Response(JSON.stringify({ paused: action === "pause" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      );

      const response = await POST(
        new Request(`http://dashboard.test/api/ops/${action}`, { method: "POST" }),
        context(action),
      );

      expect(response.status).toBe(200);
      expect(await readJson(response)).toEqual({
        paused: action === "pause",
      });
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe(`http://api.test/monitor/ops/${action}`);
      expect(init.method).toBe("POST");
      expect(init.headers["X-Monitor-Key"]).toBe("server-only-secret");
    },
  );

  it.each(["API_BASE_URL", "MONITOR_API_KEY"] as const)(
    "returns 503 without fetching when %s is missing",
    async (missing) => {
    delete process.env[missing];

    const statusResponse = await GET(
      new Request("http://dashboard.test/api/ops/status"),
      context("status"),
    );
    const pauseResponse = await POST(
      new Request("http://dashboard.test/api/ops/pause", { method: "POST" }),
      context("pause"),
    );

    expect(statusResponse.status).toBe(503);
    expect(pauseResponse.status).toBe(503);
    expect(await readJson(statusResponse)).toEqual({
      error: OPS_NOT_CONFIGURED_REASON,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("passes the upstream error status through without leaking the key", async () => {
    fetchMock.mockResolvedValue(
      new Response("secret upstream diagnostics", { status: 503 }),
    );

    const response = await POST(
      new Request("http://dashboard.test/api/ops/resume", { method: "POST" }),
      context("resume"),
    );

    expect(response.status).toBe(503);
    const text = await response.text();
    expect(text).not.toContain("server-only-secret");
    expect(text).not.toContain("secret upstream diagnostics");
  });

  it("returns 502 for invalid upstream JSON and unreachable upstreams", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("not json", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const invalid = await GET(
      new Request("http://dashboard.test/api/ops/status"),
      context("status"),
    );
    expect(invalid.status).toBe(502);

    fetchMock.mockRejectedValueOnce(new Error("ECONNREFUSED"));
    const unreachable = await GET(
      new Request("http://dashboard.test/api/ops/status"),
      context("status"),
    );
    expect(unreachable.status).toBe(502);
    const text = await unreachable.text();
    expect(text).not.toContain("server-only-secret");
  });

  it("rejects unknown actions and mismatched methods", async () => {
    const unknown = await GET(
      new Request("http://dashboard.test/api/ops/delete"),
      context("delete"),
    );
    expect(unknown.status).toBe(404);

    const wrongMethod = await GET(
      new Request("http://dashboard.test/api/ops/pause"),
      context("pause"),
    );
    expect(wrongMethod.status).toBe(405);

    const statusPost = await POST(
      new Request("http://dashboard.test/api/ops/status", { method: "POST" }),
      context("status"),
    );
    expect(statusPost.status).toBe(405);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
