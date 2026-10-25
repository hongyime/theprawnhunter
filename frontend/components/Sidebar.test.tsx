import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DashboardView } from "@/app/page";

const mockFrom = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: { from: mockFrom },
}));

import Sidebar, {
  SEARCH_DEBOUNCE_MS,
  TARGET_FETCH_LIMIT,
  type SidebarCredential,
} from "./Sidebar";

type QueryResult = {
  data: SidebarCredential[] | null;
  error: { message: string } | null;
};

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

function renderSidebar(overrides: Partial<Parameters<typeof Sidebar>[0]> = {}) {
  const props = {
    selected: null,
    activeView: "findings" as DashboardView,
    onViewChange: vi.fn(),
    onSelect: vi.fn(),
    ...overrides,
  };
  return render(<Sidebar {...props} />);
}

beforeEach(() => {
  mockFrom.mockReset();
  rows = ROWS;
  selectArgs = [];
  limitArgs = [];
  pendingResult = null;
  installMock();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Sidebar", () => {
  it("keeps the nav entries and fetches targets with the explicit 2000-row bound", async () => {
    renderSidebar({ activeView: "dashboard" });

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
    expect(screen.getByRole("button", { name: "Dashboard" })).toHaveClass("bg-white");
  });

  it("filters by bot username, chat name and chat title case-insensitively", async () => {
    const user = userEvent.setup();
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    const input = screen.getByRole("searchbox", { name: "Search targets" });

    await user.type(input, "ALPHA_BOT");
    expect(await screen.findByText("1 of 3 targets")).toBeInTheDocument();
    expect(screen.getByText("@alpha_bot / 1001")).toBeInTheDocument();
    expect(screen.queryByText("@beta_bot / 1002")).not.toBeInTheDocument();

    await user.clear(input);
    await user.type(input, "beta channel");
    expect(await screen.findByText("@beta_bot / 1002")).toBeInTheDocument();
    expect(screen.getByText("1 of 3 targets")).toBeInTheDocument();
    expect(screen.queryByText("@alpha_bot / 1001")).not.toBeInTheDocument();

    await user.clear(input);
    await user.type(input, "Gamma Group");
    expect(await screen.findByText("@gamma_bot / 1003")).toBeInTheDocument();
    expect(screen.getByText("1 of 3 targets")).toBeInTheDocument();
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
    expect(await screen.findByText("0 of 3 targets")).toBeInTheDocument();
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
    expect(await screen.findByText("0 of 3 targets")).toBeInTheDocument();
    expect(screen.getByText("No targets match")).toBeInTheDocument();
  });

  it("announces the result count politely", async () => {
    renderSidebar();
    await screen.findByText("3 of 3 targets");

    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("3 of 3 targets");
  });

  it("debounces rapid typing into a single filter pass", async () => {
    vi.useFakeTimers();
    renderSidebar();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("3 of 3 targets")).toBeInTheDocument();

    const input = screen.getByRole("searchbox", { name: "Search targets" });
    fireEvent.change(input, { target: { value: "a" } });
    fireEvent.change(input, { target: { value: "al" } });
    fireEvent.change(input, { target: { value: "alp" } });

    expect(screen.getByText("3 of 3 targets")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });

    expect(screen.getByText("1 of 3 targets")).toBeInTheDocument();
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
});
