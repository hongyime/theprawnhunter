// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockGetUser = vi.hoisted(() => vi.fn());
vi.mock("@supabase/supabase-js", () => ({
  createClient: () => ({ auth: { getUser: mockGetUser } }),
}));

import { POST, SEMANTIC_NOT_CONFIGURED_REASON } from "./route";

const savedEnv = {
  apiBase: process.env.API_BASE_URL,
  monitorKey: process.env.MONITOR_API_KEY,
  supabaseUrl: process.env.NEXT_PUBLIC_SUPABASE_URL,
  supabaseKey: process.env.NEXT_PUBLIC_SUPABASE_KEY,
};

type EnvKey =
  | "API_BASE_URL"
  | "MONITOR_API_KEY"
  | "NEXT_PUBLIC_SUPABASE_URL"
  | "NEXT_PUBLIC_SUPABASE_KEY";

function restore(key: EnvKey, value?: string) {
  if (value === undefined) {
    delete process.env[key];
  } else {
    process.env[key] = value;
  }
}

function makeRequest(body: unknown, authorized = true): Request {
  return new Request("http://dashboard.test/api/semantic-search", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(authorized ? { Authorization: "Bearer operator-jwt" } : {}),
    },
    body: JSON.stringify(body),
  });
}

async function readJson(response: Response) {
  return JSON.parse(await response.text());
}

describe("POST /api/semantic-search", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    process.env.API_BASE_URL = "http://api.test";
    process.env.MONITOR_API_KEY = "server-only-secret";
    process.env.NEXT_PUBLIC_SUPABASE_URL = "https://project.supabase.co";
    process.env.NEXT_PUBLIC_SUPABASE_KEY = "anon-key";
    mockGetUser.mockResolvedValue({
      data: { user: { id: "user-1", app_metadata: { operator: true } } },
      error: null,
    });
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    restore("API_BASE_URL", savedEnv.apiBase);
    restore("MONITOR_API_KEY", savedEnv.monitorKey);
    restore("NEXT_PUBLIC_SUPABASE_URL", savedEnv.supabaseUrl);
    restore("NEXT_PUBLIC_SUPABASE_KEY", savedEnv.supabaseKey);
  });

  it("denies an unauthenticated request without touching the upstream", async () => {
    delete process.env.API_BASE_URL;
    delete process.env.MONITOR_API_KEY;

    const response = await POST(makeRequest({ q: "bitcoin" }, false));

    expect(response.status).toBe(401);
    expect(await readJson(response)).toEqual({ error: "unauthorized" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns a 200 fallback when the proxy is not configured", async () => {
    delete process.env.API_BASE_URL;
    delete process.env.MONITOR_API_KEY;

    const response = await POST(makeRequest({ q: "bitcoin" }));

    expect(response.status).toBe(200);
    expect(await readJson(response)).toEqual({
      results: [],
      fallback: true,
      reason: SEMANTIC_NOT_CONFIGURED_REASON,
      code: "not_configured",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns a 200 fallback when only the key is missing", async () => {
    delete process.env.MONITOR_API_KEY;

    const response = await POST(makeRequest({ q: "bitcoin" }));

    expect(response.status).toBe(200);
    expect((await readJson(response)).fallback).toBe(true);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("injects X-Monitor-Key server-side and returns the upstream JSON", async () => {
    const upstreamPayload = {
      results: [{ message_id: "m-1", credential_id: "c-1", distance: 0.1 }],
      fallback: false,
      reason: null,
    };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(upstreamPayload), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await POST(
      makeRequest({ q: "bitcoin", credential_id: "c-1", limit: 20 }),
    );

    expect(response.status).toBe(200);
    expect(await readJson(response)).toEqual(upstreamPayload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/monitor/semantic-search");
    expect(init.method).toBe("POST");
    expect(init.headers["X-Monitor-Key"]).toBe("server-only-secret");
    expect(JSON.parse(init.body)).toEqual({
      q: "bitcoin",
      credential_id: "c-1",
      limit: 20,
    });
  });

  it("does not leak the key or upstream body on an upstream error", async () => {
    fetchMock.mockResolvedValue(
      new Response("secret upstream diagnostics", { status: 502 }),
    );

    const response = await POST(makeRequest({ q: "bitcoin" }));

    expect(response.status).toBe(200);
    const text = await response.text();
    expect(JSON.parse(text).fallback).toBe(true);
    expect(JSON.parse(text).code).toBe("upstream_error");
    expect(text).not.toContain("server-only-secret");
    expect(text).not.toContain("secret upstream diagnostics");
  });

  it("returns a code for an invalid upstream response", async () => {
    fetchMock.mockResolvedValue(
      new Response("not json", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await POST(makeRequest({ q: "bitcoin" }));

    expect(response.status).toBe(200);
    expect(await readJson(response)).toMatchObject({
      code: "upstream_invalid_response",
    });
  });

  it("returns a 200 fallback when the upstream is unreachable", async () => {
    fetchMock.mockRejectedValue(new Error("ECONNREFUSED"));

    const response = await POST(makeRequest({ q: "bitcoin" }));

    expect(response.status).toBe(200);
    const text = await response.text();
    expect(JSON.parse(text).fallback).toBe(true);
    expect(JSON.parse(text).code).toBe("upstream_unreachable");
    expect(text).not.toContain("server-only-secret");
  });

  it("returns a 200 fallback when the upstream times out", async () => {
    fetchMock.mockRejectedValue(
      Object.assign(new Error("timeout"), { name: "TimeoutError" }),
    );

    const response = await POST(makeRequest({ q: "bitcoin" }));

    expect(response.status).toBe(200);
    const text = await response.text();
    expect(JSON.parse(text)).toMatchObject({
      fallback: true,
      code: "upstream_timeout",
      reason: "semantic proxy upstream timeout",
    });
    expect(text).not.toContain("server-only-secret");
  });

  it("returns a 200 fallback for malformed bodies and empty queries", async () => {
    const malformed = new Request("http://dashboard.test/api/semantic-search", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer operator-jwt",
      },
      body: "{not-json",
    });

    const malformedResponse = await POST(malformed);
    expect(malformedResponse.status).toBe(200);
    expect(await readJson(malformedResponse)).toMatchObject({
      code: "invalid_request",
    });

    const emptyResponse = await POST(makeRequest({ q: "   " }));
    expect(await readJson(emptyResponse)).toMatchObject({
      fallback: true,
      code: "empty_query",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
