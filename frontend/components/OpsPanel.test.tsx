const mockAuthHeaders = vi.hoisted(() => vi.fn());

vi.mock("@/lib/api-auth", () => ({ authHeaders: mockAuthHeaders }));

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import OpsPanel, { OPS_POLL_INTERVAL_MS, type OpsStatus } from "./OpsPanel";

const runningStatus: OpsStatus = {
  paused: false,
  redis: "ok",
  pending_broadcasts: 12,
  queue_lengths: {
    celery: 3,
    scrape: 5,
    scanners: 0,
    validation: 2,
  },
  bot_count: 2,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("OpsPanel", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useRealTimers();
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    mockAuthHeaders.mockResolvedValue({
      Authorization: "Bearer test-token",
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    if (Object.prototype.hasOwnProperty.call(document, "hidden")) {
      delete (document as unknown as { hidden?: boolean }).hidden;
    }
  });

  it("renders runtime state, redis health, pending count and queue depths", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(runningStatus));

    render(<OpsPanel />);

    expect(await screen.findByLabelText("System state")).toHaveTextContent(
      "Running",
    );
    expect(screen.getByLabelText("Redis health")).toHaveTextContent("Online");
    expect(screen.getByLabelText("Pending broadcasts")).toHaveTextContent("12");
    expect(screen.getByLabelText("Queue celery")).toHaveTextContent("3");
    expect(screen.getByLabelText("Queue scrape")).toHaveTextContent("5");
    expect(screen.getByLabelText("Queue scanners")).toHaveTextContent("0");
    expect(screen.getByLabelText("Queue validation")).toHaveTextContent("2");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/ops/status",
      expect.objectContaining({
        cache: "no-store",
        headers: { Authorization: "Bearer test-token" },
      }),
    );
  });

  it("pauses optimistically, disables controls in flight, and rolls back on error", async () => {
    const post = deferred<Response>();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/ops/status")) {
        return Promise.resolve(jsonResponse(runningStatus));
      }
      if (url.endsWith("/api/ops/pause") && init?.method === "POST") {
        return post.promise;
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });

    render(<OpsPanel />);
    await screen.findByLabelText("System state");

    await userEvent.click(screen.getByRole("button", { name: "Pause" }));
    expect(
      screen.getByRole("group", { name: "Confirm pause" }),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Confirm pause" }),
    );

    // Optimistic flip happens before the server responds.
    expect(screen.getByLabelText("System state")).toHaveTextContent("Paused");
    expect(screen.getByRole("button", { name: "Confirm pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();

    await act(async () => {
      post.resolve(jsonResponse({ error: "ops upstream 503" }, 503));
    });

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("ops upstream 503");
    });
    expect(screen.getByLabelText("System state")).toHaveTextContent("Running");
    expect(screen.getByRole("button", { name: "Resume" })).toBeDisabled();
  });

  it("resumes without a confirmation and reports the result", async () => {
    const pausedStatus: OpsStatus = { ...runningStatus, paused: true };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/ops/status")) {
        return Promise.resolve(jsonResponse(pausedStatus));
      }
      if (url.endsWith("/api/ops/resume") && init?.method === "POST") {
        return Promise.resolve(jsonResponse({ paused: false }));
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`));
    });

    render(<OpsPanel />);
    await screen.findByText("Paused");

    await userEvent.click(screen.getByRole("button", { name: "Resume" }));

    await waitFor(() => {
      expect(screen.getByLabelText("System state")).toHaveTextContent("Running");
    });
    expect(screen.getByRole("status")).toHaveTextContent("System resumed.");
    const resumeCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/api/ops/resume"),
    );
    expect(resumeCall?.[1]).toMatchObject({
      method: "POST",
      headers: { Authorization: "Bearer test-token" },
    });
  });

  it("clears the 30s poll interval on unmount", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValue(jsonResponse(runningStatus));

    const { unmount } = render(<OpsPanel />);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    unmount();
    act(() => {
      vi.advanceTimersByTime(OPS_POLL_INTERVAL_MS * 2);
    });
    await flush();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops polling while the tab is hidden and resumes on visibility", async () => {
    vi.useFakeTimers();
    let hidden = false;
    Object.defineProperty(document, "hidden", {
      configurable: true,
      get: () => hidden,
    });
    fetchMock.mockResolvedValue(jsonResponse(runningStatus));

    render(<OpsPanel />);
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    hidden = true;
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    act(() => {
      vi.advanceTimersByTime(OPS_POLL_INTERVAL_MS * 2);
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    hidden = false;
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    act(() => {
      vi.advanceTimersByTime(OPS_POLL_INTERVAL_MS);
    });
    await flush();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
