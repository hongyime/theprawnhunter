// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockGetUser = vi.hoisted(() => vi.fn());
const mockCreateClient = vi.hoisted(() =>
  vi.fn(() => ({ auth: { getUser: mockGetUser } })),
);

vi.mock("@supabase/supabase-js", () => ({ createClient: mockCreateClient }));

import { requireOperator } from "./operator-auth";

const originalUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const originalKey = process.env.NEXT_PUBLIC_SUPABASE_KEY;

function requestWith(authorization?: string): Request {
  const headers = new Headers();
  if (authorization !== undefined) {
    headers.set("Authorization", authorization);
  }
  return new Request("http://localhost/api/ops/status", { headers });
}

async function denial(
  response: Response | null,
): Promise<{ status: number; body: unknown }> {
  if (!response) {
    throw new Error("expected a denial response");
  }
  return { status: response.status, body: await response.json() };
}

function userWith(operator: unknown) {
  return { id: "user-1", app_metadata: { operator } };
}

describe("requireOperator", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    process.env.NEXT_PUBLIC_SUPABASE_URL = "https://project.supabase.co";
    process.env.NEXT_PUBLIC_SUPABASE_KEY = "anon-key";
  });

  afterEach(() => {
    if (originalUrl === undefined) {
      delete process.env.NEXT_PUBLIC_SUPABASE_URL;
    } else {
      process.env.NEXT_PUBLIC_SUPABASE_URL = originalUrl;
    }
    if (originalKey === undefined) {
      delete process.env.NEXT_PUBLIC_SUPABASE_KEY;
    } else {
      process.env.NEXT_PUBLIC_SUPABASE_KEY = originalKey;
    }
  });

  it("rejects a request without an Authorization header", async () => {
    expect(await denial(await requireOperator(requestWith()))).toEqual({
      status: 401,
      body: { error: "unauthorized" },
    });
    expect(mockGetUser).not.toHaveBeenCalled();
  });

  it("rejects a malformed Authorization header", async () => {
    expect(
      await denial(await requireOperator(requestWith("Basic xyz"))),
    ).toEqual({ status: 401, body: { error: "unauthorized" } });
  });

  it("rejects a user the Auth server cannot verify", async () => {
    mockGetUser.mockResolvedValue({
      data: { user: null },
      error: { message: "bad jwt" },
    });

    expect(
      await denial(await requireOperator(requestWith("Bearer jwt-123"))),
    ).toEqual({ status: 401, body: { error: "unauthorized" } });
  });

  it("forbids a user with operator: false", async () => {
    mockGetUser.mockResolvedValue({
      data: { user: userWith(false) },
      error: null,
    });

    expect(
      await denial(await requireOperator(requestWith("Bearer jwt-123"))),
    ).toEqual({ status: 403, body: { error: "forbidden" } });
  });

  it("forbids a user with the string operator: true", async () => {
    mockGetUser.mockResolvedValue({
      data: { user: userWith("true") },
      error: null,
    });

    expect(
      await denial(await requireOperator(requestWith("Bearer jwt-123"))),
    ).toEqual({ status: 403, body: { error: "forbidden" } });
  });

  it("forbids a user with the number operator: 1", async () => {
    mockGetUser.mockResolvedValue({
      data: { user: userWith(1) },
      error: null,
    });

    expect(
      await denial(await requireOperator(requestWith("Bearer jwt-123"))),
    ).toEqual({ status: 403, body: { error: "forbidden" } });
  });

  it("allows a user whose app metadata says operator: true", async () => {
    mockGetUser.mockResolvedValue({
      data: { user: userWith(true) },
      error: null,
    });

    expect(await requireOperator(requestWith("Bearer jwt-123"))).toBeNull();
    expect(mockGetUser).toHaveBeenCalledWith("jwt-123");
    expect(mockCreateClient).toHaveBeenCalledWith(
      "https://project.supabase.co",
      "anon-key",
      {
        auth: {
          persistSession: false,
          autoRefreshToken: false,
          detectSessionInUrl: false,
        },
      },
    );
  });

  it("fails closed when Supabase env is not configured", async () => {
    delete process.env.NEXT_PUBLIC_SUPABASE_KEY;

    expect(
      await denial(await requireOperator(requestWith("Bearer jwt-123"))),
    ).toEqual({ status: 503, body: { error: "auth not configured" } });
    expect(mockGetUser).not.toHaveBeenCalled();
  });

  it("fails closed when getUser throws", async () => {
    mockGetUser.mockRejectedValue(new Error("network down"));

    expect(
      await denial(await requireOperator(requestWith("Bearer jwt-123"))),
    ).toEqual({ status: 401, body: { error: "unauthorized" } });
  });
});
