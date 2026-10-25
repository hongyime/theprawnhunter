import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DashboardView } from "@/app/page";

const mockFrom = vi.hoisted(() => vi.fn());
const mockRpc = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: { from: mockFrom, rpc: mockRpc },
}));

import Sidebar, {
  NAV_RAIL_STORAGE_KEY,
  SEARCH_DEBOUNCE_MS,
  SERVER_SEARCH_LIMIT,
  TARGET_FETCH_LIMIT,
  type SidebarCredential,
} from "./Sidebar";

type QueryResult = {
  data: SidebarCredential[] | null;
  error: { message: string } | null;
};

type RpcResult = {
  data: unknown;
  error: { message: string } | null;
};

type RpcCall = { name: string; args: { q: string; lim: number } };

type Chain = {
  select: (columns: string) => Chain;
  not: (...args: unknown[]) => Chain;
  order: (...args: unknown[]) => Chain;
  limit: (count: number) => Promise<QueryResult>;
};

const ROWS: SidebarCredential[] = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    created_at: "2026-10-01T00:00:00Z",
    source: "shodan",
    status: "active",
    collection_yield_score: 0.9,
    chat_member_count: 1200,
    chat_name: "Alpha HQ",
    chat_type: "supergroup",
    meta: { bot_username: "alpha_bot", bot_id: "1001", chat_title: "Alpha Ops" },
  },
  {
    id: "22222222-2222-2222-2222-222222222222",
    created_at: "2026-10-02T00:00:00Z",
    source: "github",
    status: "revoked",
    collection_yield_score: 0.5,
    chat_name: "Beta Channel",
    chat_type: "channel",
    meta: { bot_username: "beta_bot", bot_id: "1002", chat_title: "Beta Wire" },
  },
  {
    id: "33333333-3333-3333-3333-333333333333",
    created_at: "2026-10-03T00:00:00Z",
    source: "fofa",
    status: "pending",
    chat_name: "Gamma HQ",
    chat_type: "group",
    meta: { bot_username: "gamma_bot", bot_id: "1003", chat_title: "Gamma Group" },
  },
];

let rows: SidebarCredential[] = [];
let selectArgs: string[] = [];
let limitArgs: number[] = [];
let pendingResult: Promise<QueryResult> | null = null;
let rpcCalls: RpcCall[] = [];

function installMock() {
  mockFrom.mockImplementation(() => {
    const chain = {} as Chain;
    chain.select = vi.fn((columns: string) => {
      selectArgs.push(columns);
      return chain;
    });
    chain.not = vi.fn(() => chain);
    chain.order = vi.fn(() => chain);
    chain.limit = vi.fn((count: number) => {
      limitArgs.push(count);
      return pendingResult ?? Promise.resolve({ data: rows, error: null });
    });
    return chain;
  });
}

function serverMatches(query: string): SidebarCredential[] {
  const needle = query.trim().toLowerCase();
  return rows.filter((cred) =>
    [cred.meta?.bot_username, cred.meta?.chat_title, cred.chat_name].some(
      (value) => typeof value === "string" && value.toLowerCase().includes(needle),
    ),
  );
}

function installRpcMock() {
  mockRpc.mockImplementation(
    async (name: string, args: { q: string; lim: number }): Promise<RpcResult> => {
      rpcCalls.push({ name, args });
      return { data: serverMatches(args.q), error: null };
    },
  );
}

function renderSidebar(overrides: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const props = {
    selected: null,
    activeView: "chat" as DashboardView,
    onViewChange: vi.fn(),
    onSelect: vi.fn(),
    ...overrides,
  };
  return render(<Sidebar {...props} />);
}

async function flushAsync() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  window.localStorage.clear();
  mockFrom.mockReset();
  mockRpc.mockReset();
  rows = ROWS;
  selectArgs = [];
  limitArgs = [];
  pendingResult = null;
  rpcCalls = [];
  installMock();
  installRpcMock();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Sidebar", () => {
  it("keeps the nav entries and fetches targets with the explicit 2000-row bound", async () => {
    renderSidebar({ activeView: "chat" });

    expect(await screen.findByText("3 of 3 targets")).toBeInTheDocument();

    for (const name of [
      "Findings",
      "Search",
      "Watchlists",
      "Chat Feed",
      "Bot Intel",
      "All Telemetry",
      "Dashboard",
    ]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }

    expect(limitArgs).toEqual([TARGET_FETCH_LIMIT]);
    expect(selectArgs[0]).toContain("status");
    expect(selectArgs[0]).toContain("chat_type");
    expect(mockRpc).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Chat Feed" })).toHaveClass("bg-white");
  });

  it("filters by bot username, chat name and chat title case-insensitively", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    const input = screen.getByRole("searchbox", { name: "Search targets" });

    await user.type(input, "ALPHA_BOT");
    expect(await screen.findByText("1 of 1 targets")).toBeInTheDocument();
    expect(screen.getByText("@alpha_bot / 1001")).toBeInTheDocument();
    expect(screen.queryByText("@beta_bot / 1002")).not.toBeInTheDocument();

    await user.clear(input);
    await user.type(input, "beta channel");
    expect(await screen.findByText("@beta_bot / 1002")).toBeInTheDocument();
    expect(screen.getByText("1 of 1 targets")).toBeInTheDocument();
    expect(screen.queryByText("@alpha_bot / 1001")).not.toBeInTheDocument();

    await user.clear(input);
    await user.type(input, "Gamma Group");
    expect(await screen.findByText("@gamma_bot / 1003")).toBeInTheDocument();
    expect(screen.getByText("1 of 1 targets")).toBeInTheDocument();
  });

  it("combines a status chip with the text query using AND semantics", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    const activeChip = screen.getByRole("button", { name: "active" });
    await user.click(activeChip);
    expect(activeChip).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("1 of 3 targets")).toBeInTheDocument();

    const input = screen.getByRole("searchbox", { name: "Search targets" });
    await user.type(input, "beta");
    expect(await screen.findByText("0 of 1 targets")).toBeInTheDocument();
    expect(screen.getByText("No targets match")).toBeInTheDocument();
  });

  it("combines status and chat-type chips", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    await user.click(screen.getByRole("button", { name: "active" }));
    expect(await screen.findByText("1 of 3 targets")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "group" }));
    expect(await screen.findByText("0 of 3 targets")).toBeInTheDocument();
    expect(screen.getByText("No targets match")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "active" }));
    expect(await screen.findByText("1 of 3 targets")).toBeInTheDocument();
    expect(screen.getByText("@gamma_bot / 1003")).toBeInTheDocument();
  });

  it("shows the zero-match empty state when the query matches nothing", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    await user.type(screen.getByRole("searchbox", { name: "Search targets" }), "zzz-no-such");
    expect(await screen.findByText("0 of 0 targets")).toBeInTheDocument();
    expect(screen.getByText("No targets match")).toBeInTheDocument();
  });

  it("announces the result count politely", async () => {
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("3 of 3 targets");
  });

  it("debounces rapid typing into exactly one RPC call", async () => {
    vi.useFakeTimers();
    renderSidebar();
    await flushAsync();
    expect(screen.getByText("3 of 3 targets")).toBeInTheDocument();

    const input = screen.getByRole("searchbox", { name: "Search targets" });
    fireEvent.change(input, { target: { value: "a" } });
    fireEvent.change(input, { target: { value: "al" } });
    fireEvent.change(input, { target: { value: "alp" } });

    expect(screen.getByText("3 of 3 targets")).toBeInTheDocument();
    expect(mockRpc).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    await flushAsync();

    expect(mockRpc).toHaveBeenCalledTimes(1);
    expect(mockRpc).toHaveBeenCalledWith("search_targets", {
      q: "alp",
      lim: SERVER_SEARCH_LIMIT,
    });
    expect(screen.getByText("1 of 1 targets")).toBeInTheDocument();
    expect(screen.getByText("@alpha_bot / 1001")).toBeInTheDocument();
  });

  it("keeps client filtering below the two-character server threshold", async () => {
    vi.useFakeTimers();
    renderSidebar();
    await flushAsync();

    const input = screen.getByRole("searchbox", { name: "Search targets" });
    fireEvent.change(input, { target: { value: "a" } });
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    await flushAsync();

    expect(mockRpc).not.toHaveBeenCalled();
    expect(screen.getByText("3 of 3 targets")).toBeInTheDocument();
  });

  it("renders RPC rows even when the client text matcher would exclude them", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    mockRpc.mockResolvedValueOnce({ data: [ROWS[0]], error: null });

    await user.type(screen.getByRole("searchbox", { name: "Search targets" }), "zzz");

    expect(await screen.findByText("1 of 1 targets")).toBeInTheDocument();
    expect(screen.getByText("@alpha_bot / 1001")).toBeInTheDocument();
    expect(screen.queryByText("@beta_bot / 1002")).not.toBeInTheDocument();
  });

  it("applies status and chat-type chips to the server result set", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    mockRpc.mockImplementation(
      async (name: string, args: { q: string; lim: number }): Promise<RpcResult> => {
        rpcCalls.push({ name, args });
        return { data: [ROWS[0], ROWS[1]], error: null };
      },
    );

    await user.type(screen.getByRole("searchbox", { name: "Search targets" }), "bot");
    expect(await screen.findByText("2 of 2 targets")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "active" }));
    expect(await screen.findByText("1 of 2 targets")).toBeInTheDocument();
    expect(screen.getByText("@alpha_bot / 1001")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "channel" }));
    expect(await screen.findByText("0 of 2 targets")).toBeInTheDocument();
    expect(screen.getByText("No targets match")).toBeInTheDocument();
  });

  it("falls back to client filtering with an inline note when the RPC fails", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    mockRpc.mockImplementation(
      async (name: string, args: { q: string; lim: number }): Promise<RpcResult> => {
        rpcCalls.push({ name, args });
        return {
          data: null,
          error: { message: "permission denied for function search_targets" },
        };
      },
    );

    await user.type(screen.getByRole("searchbox", { name: "Search targets" }), "beta");

    expect(await screen.findByTestId("server-search-note")).toHaveTextContent(
      "Server search unavailable",
    );
    expect(screen.getByText("1 of 3 targets")).toBeInTheDocument();
    expect(screen.getByText("@beta_bot / 1002")).toBeInTheDocument();
    expect(screen.queryByText("@alpha_bot / 1001")).not.toBeInTheDocument();
    expect(screen.queryByText("No targets match")).not.toBeInTheDocument();
  });

  it("shows a loading state until the target fetch resolves", async () => {
    let release!: (value: QueryResult) => void;
    pendingResult = new Promise<QueryResult>((resolve) => {
      release = resolve;
    });

    renderSidebar();
    expect(screen.getByText("Loading targets…")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading targets…");

    await act(async () => {
      release({ data: ROWS, error: null });
    });

    expect(await screen.findByText("3 of 3 targets")).toBeInTheDocument();
    expect(screen.queryByText("Loading targets…")).not.toBeInTheDocument();
  });

  it("collapses by default and exposes all seven destinations without visible labels", async () => {
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    for (const label of [
      "Findings",
      "Search",
      "Watchlists",
      "Chat Feed",
      "Bot Intel",
      "All Telemetry",
      "Dashboard",
    ]) {
      const button = screen.getByRole("button", { name: label });
      expect(button).toHaveAttribute("title", label);
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }

    expect(
      screen.getByRole("button", { name: "Expand navigation" }),
    ).toBeInTheDocument();
  });

  it("expands to show labels and persists the state across a remount", async () => {
    const user = userEvent.setup();
    const { unmount } = renderSidebar();
    await screen.findByText("3 of 3 targets");

    await user.click(screen.getByRole("button", { name: "Expand navigation" }));

    expect(screen.getByText("Findings")).toBeInTheDocument();
    expect(window.localStorage.getItem(NAV_RAIL_STORAGE_KEY)).toBe("expanded");

    unmount();
    renderSidebar();

    expect(await screen.findByText("Findings")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Collapse navigation" }),
    ).toBeInTheDocument();
  });

  it.each(["chat", "botTelemetry"] as const)(
    "renders the targets panel for the %s view",
    async (view) => {
      renderSidebar({ activeView: view });

      expect(await screen.findByText("Drill-down targets")).toBeInTheDocument();
      expect(
        screen.getByRole("searchbox", { name: "Search targets" }),
      ).toBeInTheDocument();
    },
  );

  it.each([
    "findings",
    "search",
    "watchlists",
    "dashboard",
    "globalTelemetry",
  ] as const)("omits the targets panel for the %s view", async (view) => {
    renderSidebar({ activeView: view });
    await flushAsync();

    expect(screen.queryByText("Drill-down targets")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("searchbox", { name: "Search targets" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("keeps the target filter text when switching views and back", async () => {
    const user = userEvent.setup();
    const props = { selected: null, onViewChange: vi.fn(), onSelect: vi.fn() };
    const { rerender } = render(<Sidebar {...props} activeView="chat" />);
    await screen.findByText("3 of 3 targets");

    const input = screen.getByRole("searchbox", { name: "Search targets" });
    await user.type(input, "alpha");
    expect(await screen.findByText("1 of 1 targets")).toBeInTheDocument();

    rerender(<Sidebar {...props} activeView="findings" />);
    expect(
      screen.queryByRole("searchbox", { name: "Search targets" }),
    ).not.toBeInTheDocument();

    rerender(<Sidebar {...props} activeView="chat" />);
    expect(screen.getByRole("searchbox", { name: "Search targets" })).toHaveValue(
      "alpha",
    );
    expect(await screen.findByText("1 of 1 targets")).toBeInTheDocument();
  });
});
