// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockGetUser = vi.hoisted(() => vi.fn());
vi.mock("@supabase/supabase-js", () => ({
  createClient: () => ({ auth: { getUser: mockGetUser } }),
}));

import { GET } from "./route";

const HIT_ID = "80000000-0000-4000-8000-000000000001";

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

function authedRequest(url: string, init: RequestInit = {}): Request {
  return new Request(url, {
    ...init,
    headers: {
      ...(init.headers as Record<string, string> | undefined),
      Authorization: "Bearer operator-jwt",
    },
  });
}

function linkRequest(hitId: string): Request {
  return authedRequest(
    `http://dashboard.test/api/watchlist-hit-link?hit_id=${hitId}`,
  );
}

async function readJson(response: Response) {
  return JSON.parse(await response.text());
}

describe("GET /api/watchlist-hit-link", () => {
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

  it("proxies a valid hit to the upstream link path with the key", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ url: "https://t.me/c/1/2" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await GET(linkRequest(HIT_ID));

    expect(response.status).toBe(200);
    expect(await readJson(response)).toEqual({ url: "https://t.me/c/1/2" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`http://api.test/monitor/watchlist/hits/${HIT_ID}/link`);
    expect(init.method).toBe("GET");
    expect(init.headers["X-Monitor-Key"]).toBe("server-only-secret");
  });

  it("denies an unauthenticated request without touching the upstream", async () => {
    delete process.env.API_BASE_URL;
    delete process.env.MONITOR_API_KEY;

    const response = await GET(
      new Request(`http://dashboard.test/api/watchlist-hit-link?hit_id=${HIT_ID}`),
    );

    expect(response.status).toBe(401);
    expect(await readJson(response)).toEqual({ error: "unauthorized" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 400 for an invalid hit id without fetching", async () => {
    const response = await GET(linkRequest("not-a-uuid"));

    expect(response.status).toBe(400);
    expect(await readJson(response)).toEqual({ url: null });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns a null url when the proxy is not configured", async () => {
    delete process.env.API_BASE_URL;
    delete process.env.MONITOR_API_KEY;

    const response = await GET(linkRequest(HIT_ID));

    expect(response.status).toBe(200);
    expect(await readJson(response)).toEqual({ url: null });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
