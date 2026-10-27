import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockRpc = vi.hoisted(() => vi.fn());
const mockAuthHeaders = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    rpc: mockRpc,
  },
}));

vi.mock("@/lib/api-auth", () => ({ authHeaders: mockAuthHeaders }));

import WatchlistHitsView, { type WatchlistHit } from "./WatchlistHitsView";

const namedHit: WatchlistHit = {
  hit_id: "80000000-0000-4000-8000-000000000001",
  rule_id: "70000000-0000-4000-8000-000000000001",
  rule_name: "AWS keys",
  message_id: "60000000-0000-4000-8000-000000000001",
  credential_id: "30000000-0000-4000-8000-000000000001",
  matched_text: "AKIAEXAMPLE",
  telegram_msg_id: 456,
  created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
};

const deletedRuleHit: WatchlistHit = {
  hit_id: "80000000-0000-4000-8000-000000000002",
  rule_id: "70000000-0000-4000-8000-0000000000ff",
  rule_name: null,
  message_id: "60000000-0000-4000-8000-000000000002",
  credential_id: null,
  matched_text: "<script>alert(1)</script> orphaned match",
  telegram_msg_id: 457,
  created_at: new Date(Date.now() - 90 * 60_000).toISOString(),
};

const LINK = "https://t.me/c/1234567890/789/456";

function hitResult(hits: WatchlistHit[]) {
  return { data: hits, error: null };
}

describe("WatchlistHitsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockAuthHeaders.mockResolvedValue({
      Authorization: "Bearer test-token",
    });
    mockRpc.mockResolvedValue(hitResult([namedHit, deletedRuleHit]));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders rows from the operator RPC with names, credential scope and escaped matches", async () => {
    const { container } = render(<WatchlistHitsView />);

    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledWith("list_watchlist_hits", { lim: 50 });
    expect(screen.getByRole("cell", { name: /deleted rule/ })).toBeInTheDocument();
    expect(screen.getByText("70000000")).toBeInTheDocument();
    expect(screen.getByText(/AKIAEXAMPLE/)).toBeInTheDocument();
    expect(screen.getByText(/<script>alert\(1\)<\/script>/)).toBeInTheDocument();
    // A literal `<script>` in matched_text is text, not markup.
    expect(container.querySelector("script")).toBeNull();
    expect(screen.getByTitle("Global rule")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /open in telegram/i })).toHaveLength(2);
  });

  it("shows the empty state when no hits exist", async () => {
    mockRpc.mockResolvedValue(hitResult([]));

    render(<WatchlistHitsView />);

    expect(await screen.findByText("No watchlist hits yet")).toBeInTheDocument();
  });

  it("shows the load error and retries", async () => {
    mockRpc
      .mockResolvedValueOnce({ data: null, error: { message: "boom" } })
      .mockResolvedValue(hitResult([namedHit]));

    render(<WatchlistHitsView />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Watchlist hits could not be loaded: boom",
    );
    expect(mockRpc).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledTimes(2);
  });

  it("re-queries when the limit changes", async () => {
    render(<WatchlistHitsView />);
    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Limit"), "25");

    await waitFor(() => {
      expect(mockRpc).toHaveBeenLastCalledWith("list_watchlist_hits", {
        lim: 25,
      });
    });
  });

  it("filters hits by rule name on the client", async () => {
    render(<WatchlistHitsView />);
    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Rule"), "AWS keys");

    expect(screen.queryByRole("cell", { name: /deleted rule/ })).toBeNull();
    expect(screen.getByRole("cell", { name: "AWS keys" })).toBeInTheDocument();
  });

  it("asks the server proxy for the deep link and opens the returned URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ url: LINK }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    render(<WatchlistHitsView />);
    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();

    await userEvent.click(
      screen.getAllByRole("button", { name: /open in telegram/i })[0],
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `/api/watchlist-hit-link?hit_id=${encodeURIComponent(namedHit.hit_id)}`,
        expect.objectContaining({
          method: "GET",
          cache: "no-store",
          headers: { Authorization: "Bearer test-token" },
        }),
      );
    });
    await waitFor(() => {
      expect(openSpy).toHaveBeenCalledWith(
        LINK,
        "_blank",
        "noopener,noreferrer",
      );
    });
  });

  it("reports a missing link on 404 without opening a window", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 404,
      json: async () => ({ url: null, code: "not_found" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    render(<WatchlistHitsView />);
    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();

    await userEvent.click(
      screen.getAllByRole("button", { name: /open in telegram/i })[0],
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "No Telegram link is available for this hit.",
    );
    expect(openSpy).not.toHaveBeenCalled();
  });

  it("surfaces the tunnel-down message on a 502 link lookup", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      json: async () => ({ url: null, code: "upstream_unreachable" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);

    render(<WatchlistHitsView />);
    expect(await screen.findByRole("cell", { name: "AWS keys" })).toBeInTheDocument();

    await userEvent.click(
      screen.getAllByRole("button", { name: /open in telegram/i })[0],
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "network tunnel may be offline",
    );
    expect(openSpy).not.toHaveBeenCalled();
  });
});
