import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockFrom = vi.hoisted(() => vi.fn());
const mockUseAuth = vi.hoisted(() => vi.fn());
const mockRpc = vi.hoisted(() => vi.fn());
const mockChannel = vi.hoisted(() => vi.fn());
const mockRemoveChannel = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    from: mockFrom,
    rpc: mockRpc,
    channel: mockChannel,
    removeChannel: mockRemoveChannel,
  },
}));

vi.mock("@/lib/auth", () => ({
  useAuth: mockUseAuth,
}));

import type { Credential } from "@/app/page";
import ChatWindow from "./ChatWindow";

const PAGE_SIZE = 50;
const BASE_TIME = Date.parse("2026-09-05T12:00:00Z");

type BroadcastHandler = (event: { payload?: Record<string, unknown> }) => void;
type StatusHandler = (status: string) => void;

let broadcastHandler: BroadcastHandler | undefined;
let subscribeStatusHandler: StatusHandler | undefined;
let channelMock:
  | { on: ReturnType<typeof vi.fn>; subscribe: ReturnType<typeof vi.fn> }
  | undefined;

const credential = {
  id: "30000000-0000-0000-0000-000000000001",
  source: "test",
  created_at: "2026-09-05T00:00:00Z",
  meta: {},
} as Credential;

function makeMessage(id: string, content: string, created_at: string) {
  return {
    id,
    credential_id: credential.id,
    sender_pseudonym: `user_${id}`,
    content,
    media_type: "text",
    is_broadcasted: false,
    created_at,
  };
}

function makePage(prefix: string, count: number, startMs = BASE_TIME) {
  return Array.from({ length: count }, (_, index) =>
    makeMessage(
      `${prefix}-${index}`,
      `${prefix}-msg-${index}`,
      new Date(startMs - index * 60_000).toISOString(),
    ),
  );
}

const liveMessage = {
  ...makeMessage("message-live", "live feed message", "2026-09-05T02:00:00Z"),
};

const searchRow = {
  id: "60000000-0000-0000-0000-000000000001",
  credential_id: credential.id,
  sender_key: "user_ab12cd34",
  sender_pseudonym: "user_9f8e7d6c",
  snippet: "Transfer <b>bitcoin</b> to the wallet",
  media_type: "text",
  created_at: new Date(Date.now() - 90 * 60_000).toISOString(),
};

function messageBuilder(result: { data: unknown[] | null; error: unknown }) {
  const builder = {
    select: vi.fn(),
    eq: vi.fn(),
    order: vi.fn(),
    range: vi.fn(),
  };
  builder.select.mockReturnValue(builder);
  builder.eq.mockReturnValue(builder);
  builder.order.mockReturnValue(builder);
  builder.range.mockResolvedValue(result);
  return builder;
}

function mockFeed(messages: unknown[] = [liveMessage]) {
  mockFrom.mockReturnValue(messageBuilder({ data: messages, error: null }));
}

function messageLog() {
  return screen.getByRole("log", { name: /message history/i });
}

function triggerPoll() {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => "visible",
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

function searchInput() {
  return screen.getByLabelText("Search this chat");
}

async function typeQuery(query: string) {
  await userEvent.type(searchInput(), query, { delay: null });
}

describe("ChatWindow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    mockUseAuth.mockReturnValue({ session: { user: { id: "operator" } } });
    mockRpc.mockResolvedValue({ data: [], error: null });
    broadcastHandler = undefined;
    subscribeStatusHandler = undefined;
    channelMock = undefined;
    mockRemoveChannel.mockResolvedValue("ok");
    mockChannel.mockImplementation(() => {
      const channel = {
        on: vi.fn((_type: string, _filter: unknown, handler: BroadcastHandler) => {
          broadcastHandler = handler;
          return channel;
        }),
        subscribe: vi.fn((handler: StatusHandler) => {
          subscribeStatusHandler = handler;
          return channel;
        }),
      };
      channelMock = channel;
      return channel;
    });
  });

  it("does not query evidence without an authenticated session", async () => {
    mockUseAuth.mockReturnValue({ session: null });
    render(<ChatWindow credential={credential} />);

    expect(await screen.findByText("Sign in required to view exfiltrated messages.")).toBeInTheDocument();
    expect(mockFrom).not.toHaveBeenCalled();
  });

  it("fetches one newest-first page with a bounded range and renders it chronologically", async () => {
    mockUseAuth.mockReturnValue({ session: { user: { id: "operator" } } });
    const builder = messageBuilder({
      data: [
        makeMessage("new", "newest message", "2026-09-05T02:00:00Z"),
        makeMessage("old", "older message", "2026-09-05T01:00:00Z"),
      ],
      error: null,
    });
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("newest message");
    expect(mockFrom).toHaveBeenCalledWith("evidence_redacted");
    expect(builder.select).toHaveBeenCalledWith("*");
    expect(builder.order).toHaveBeenCalledWith("created_at", { ascending: false });
    expect(builder.range).toHaveBeenCalledWith(0, PAGE_SIZE - 1);
    const messages = screen.getAllByText(/(older|newest) message/);
    expect(messages.map((node) => node.textContent)).toEqual([
      "older message",
      "newest message",
    ]);
  });

  it("turns permission failures into the sign-in state", async () => {
    mockUseAuth.mockReturnValue({ session: { user: { id: "operator" } } });
    mockFrom.mockReturnValue(
      messageBuilder({ data: [], error: { message: "row-level security denied", code: "42501" } }),
    );
    render(<ChatWindow credential={credential} />);

    await waitFor(() => {
      expect(screen.getByText("Sign in required to view exfiltrated messages.")).toBeInTheDocument();
    });
  });

  it("prepends an older page, deduped by id, and keeps the feed chronological", async () => {
    const firstPage = makePage("page1", PAGE_SIZE);
    const olderPage = [
      firstPage[PAGE_SIZE - 1],
      makeMessage("older-b", "older message b", new Date(BASE_TIME - 51 * 60_000).toISOString()),
      makeMessage("older-a", "older message a", new Date(BASE_TIME - 52 * 60_000).toISOString()),
    ];
    const builder = messageBuilder({ data: firstPage, error: null });
    builder.range
      .mockResolvedValueOnce({ data: firstPage, error: null })
      .mockResolvedValueOnce({ data: olderPage, error: null });
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("page1-msg-0");
    expect(screen.queryByText("Beginning of history")).not.toBeInTheDocument();

    fireEvent.scroll(messageLog());

    await screen.findByText("older message a");
    expect(builder.range).toHaveBeenLastCalledWith(PAGE_SIZE, PAGE_SIZE * 2 - 1);
    expect(screen.getAllByText("page1-msg-49")).toHaveLength(1);

    const rendered = screen.getAllByText(/page1-msg-\d+|older message [ab]/);
    expect(rendered[0].textContent).toBe("older message a");
    expect(rendered[1].textContent).toBe("older message b");
    expect(rendered[2].textContent).toBe("page1-msg-49");
  });

  it("marks the beginning of history when a page returns fewer than the page size", async () => {
    mockFeed([liveMessage]);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("live feed message");
    expect(await screen.findByText("Beginning of history")).toBeInTheDocument();
    expect(mockFrom).toHaveBeenCalledTimes(1);

    fireEvent.scroll(messageLog());
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(mockFrom).toHaveBeenCalledTimes(1);
  });

  it("collapses rapid top-scroll triggers into one in-flight page request", async () => {
    const firstPage = makePage("page1", PAGE_SIZE);
    let resolveOlder: ((value: unknown) => void) | undefined;
    const builder = messageBuilder({ data: firstPage, error: null });
    builder.range
      .mockResolvedValueOnce({ data: firstPage, error: null })
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOlder = resolve;
          }),
      );
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("page1-msg-0");

    fireEvent.scroll(messageLog());
    fireEvent.scroll(messageLog());
    fireEvent.scroll(messageLog());

    await waitFor(() => expect(builder.range).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(builder.range).toHaveBeenCalledTimes(2);

    resolveOlder?.({ data: [], error: null });
    expect(await screen.findByText("Beginning of history")).toBeInTheDocument();
  });

  it("refreshes only the newest page on poll and merges new messages by id", async () => {
    const firstPage = [
      makeMessage("base-b", "base message b", "2026-09-05T02:00:00Z"),
      makeMessage("base-a", "base message a", "2026-09-05T01:00:00Z"),
    ];
    const polledPage = [
      makeMessage("fresh", "brand new message", "2026-09-05T03:00:00Z"),
      ...firstPage,
    ];
    const builder = messageBuilder({ data: firstPage, error: null });
    builder.range
      .mockResolvedValueOnce({ data: firstPage, error: null })
      .mockResolvedValueOnce({ data: polledPage, error: null });
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("base message b");
    expect(builder.range).toHaveBeenCalledTimes(1);

    const scrollSpy = Element.prototype.scrollIntoView as unknown as { mockClear: () => void };
    scrollSpy.mockClear();

    triggerPoll();

    expect(await screen.findByText("brand new message")).toBeInTheDocument();
    expect(builder.range).toHaveBeenLastCalledWith(0, PAGE_SIZE - 1);
    expect(screen.getAllByText("base message b")).toHaveLength(1);
    expect(screen.getAllByText("base message a")).toHaveLength(1);
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it("shows an inline retry row after a page error and recovers on retry", async () => {
    const firstPage = makePage("page1", PAGE_SIZE);
    const olderPage = makePage("page2", 2, BASE_TIME - PAGE_SIZE * 60_000);
    const builder = messageBuilder({ data: firstPage, error: null });
    builder.range
      .mockResolvedValueOnce({ data: firstPage, error: null })
      .mockResolvedValueOnce({ data: null, error: { message: "boom" } })
      .mockResolvedValueOnce({ data: olderPage, error: null });
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("page1-msg-0");
    fireEvent.scroll(messageLog());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Failed to load older messages: boom");
    expect(screen.queryByText("Loading earlier messages…")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(await screen.findByText("page2-msg-0")).toBeInTheDocument();
    expect(builder.range).toHaveBeenCalledTimes(3);
    expect(builder.range).toHaveBeenLastCalledWith(PAGE_SIZE, PAGE_SIZE * 2 - 1);
  });

  it("restores the paginated feed after clearing the search", async () => {
    const firstPage = makePage("page1", PAGE_SIZE);
    mockFeed(firstPage);
    mockRpc.mockResolvedValue({ data: [searchRow], error: null });
    render(<ChatWindow credential={credential} />);

    await screen.findByText("page1-msg-0");
    expect(screen.queryByText("Beginning of history")).not.toBeInTheDocument();

    await typeQuery("bitcoin");
    await screen.findByText("bitcoin", undefined, { timeout: 2000 });
    expect(screen.queryByText("page1-msg-0")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /clear search/i }));

    expect(await screen.findByText("page1-msg-0")).toBeInTheDocument();
    expect(screen.queryByText("bitcoin")).not.toBeInTheDocument();
  });

  it("debounces typing into a single chat-scoped search_evidence call", async () => {
    mockFeed();
    mockRpc.mockResolvedValue({ data: [searchRow], error: null });
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("bitcoin");

    await waitFor(
      () => {
        expect(mockRpc).toHaveBeenCalledTimes(1);
      },
      { timeout: 2000 },
    );
    expect(mockRpc).toHaveBeenCalledWith("search_evidence", {
      q: "bitcoin",
      credential_id: credential.id,
      lim: 50,
    });
  });

  it("does not search until the query reaches two characters", async () => {
    mockFeed();
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("b");
    await new Promise((resolve) => setTimeout(resolve, 450));

    expect(mockRpc).not.toHaveBeenCalled();
    expect(screen.getByText("live feed message")).toBeInTheDocument();
  });

  it("renders search results in place of the feed with sender, time, and highlights", async () => {
    mockFeed();
    mockRpc.mockResolvedValue({ data: [searchRow], error: null });
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("bitcoin");

    const mark = await screen.findByText("bitcoin", undefined, { timeout: 2000 });
    expect(mark.tagName).toBe("MARK");
    expect(screen.getByText("user_ab12cd34")).toBeInTheDocument();
    expect(screen.getByText("user_9f8e7d6c")).toBeInTheDocument();
    const time = document.querySelector("time");
    expect(time).not.toBeNull();
    expect(time).toHaveAttribute("datetime", searchRow.created_at);
    expect(screen.getByText("1 match in this chat")).toBeInTheDocument();
    expect(screen.queryByText("live feed message")).not.toBeInTheDocument();
  });

  it("clear and Escape both restore the live feed", async () => {
    mockFeed();
    mockRpc.mockResolvedValue({ data: [searchRow], error: null });
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("bitcoin");
    await screen.findByText("bitcoin", undefined, { timeout: 2000 });
    await userEvent.click(screen.getByRole("button", { name: /clear search/i }));

    expect(searchInput()).toHaveValue("");
    expect(await screen.findByText("live feed message")).toBeInTheDocument();

    await typeQuery("bitcoin");
    await screen.findByText("bitcoin", undefined, { timeout: 2000 });
    await userEvent.keyboard("{Escape}");

    expect(searchInput()).toHaveValue("");
    expect(await screen.findByText("live feed message")).toBeInTheDocument();
  });

  it("shows an explicit no-match empty state with a way back", async () => {
    mockFeed();
    mockRpc.mockResolvedValue({ data: [], error: null });
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("zzzz-no-hits");

    expect(
      await screen.findByText("No matches in this chat", undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
    expect(screen.getByText("0 matches in this chat")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /back to the conversation/i }));
    expect(await screen.findByText("live feed message")).toBeInTheDocument();
  });

  it("surfaces a search RPC error with a working Retry button", async () => {
    mockFeed();
    mockRpc
      .mockResolvedValueOnce({ data: null, error: { message: "relation missing" } })
      .mockResolvedValueOnce({ data: [searchRow], error: null });
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("bitcoin");

    const alert = await screen.findByRole("alert", undefined, { timeout: 2000 });
    expect(alert).toHaveTextContent("Search failed: relation missing");
    expect(screen.queryByText("No matches in this chat")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(await screen.findByText("bitcoin", undefined, { timeout: 2000 })).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders hostile snippet markup as inert text", async () => {
    mockFeed();
    mockRpc.mockResolvedValue({
      data: [
        {
          ...searchRow,
          snippet:
            'Pay <b>bitcoin</b> <img src=x onerror="alert(1)"> <script>alert(2)</script>',
        },
      ],
      error: null,
    });
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await typeQuery("bitcoin");

    const mark = await screen.findByText("bitcoin", undefined, { timeout: 2000 });
    expect(mark.tagName).toBe("MARK");
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector("script")).toBeNull();
    expect(document.body.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(document.body.textContent).toContain("<script>alert(2)</script>");
  });

  it("focuses the in-chat search input when `/` is pressed", async () => {
    mockFeed();
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    await userEvent.keyboard("/");

    expect(searchInput()).toHaveFocus();
  });

  it("subscribes to the private evidence channel", async () => {
    mockFeed();
    render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    expect(mockChannel).toHaveBeenCalledTimes(1);
    expect(mockChannel).toHaveBeenCalledWith("evidence", { config: { private: true } });
    expect(channelMock?.subscribe).toHaveBeenCalledTimes(1);
  });

  it("refetches only the newest page once when an evidence insert arrives", async () => {
    const firstPage = [makeMessage("base", "base message", "2026-09-05T02:00:00Z")];
    const refreshed = [
      makeMessage("fresh", "brand new message", "2026-09-05T03:00:00Z"),
      ...firstPage,
    ];
    const builder = messageBuilder({ data: firstPage, error: null });
    builder.range
      .mockResolvedValueOnce({ data: firstPage, error: null })
      .mockResolvedValueOnce({ data: refreshed, error: null });
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("base message");
    expect(builder.range).toHaveBeenCalledTimes(1);

    await act(async () => {
      broadcastHandler?.({
        payload: { credential_id: credential.id, message_id: "fresh" },
      });
    });

    expect(await screen.findByText("brand new message")).toBeInTheDocument();
    expect(builder.range).toHaveBeenCalledTimes(2);
    expect(builder.range).toHaveBeenLastCalledWith(0, PAGE_SIZE - 1);
    expect(screen.getAllByText("base message")).toHaveLength(1);
  });

  it("falls back to the 3s poll when the realtime subscription errors, without a banner", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const firstPage = [makeMessage("base", "base message", "2026-09-05T02:00:00Z")];
      const refreshed = [
        makeMessage("fresh", "poll fallback message", "2026-09-05T03:00:00Z"),
        ...firstPage,
      ];
      const builder = messageBuilder({ data: firstPage, error: null });
      builder.range
        .mockResolvedValueOnce({ data: firstPage, error: null })
        .mockResolvedValueOnce({ data: refreshed, error: null });
      mockFrom.mockReturnValue(builder);
      render(<ChatWindow credential={credential} />);

      await screen.findByText("base message");

      act(() => {
        subscribeStatusHandler?.("CHANNEL_ERROR");
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(3000);
      });

      expect(await screen.findByText("poll fallback message")).toBeInTheDocument();
      expect(builder.range).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("removes the realtime channel on unmount", async () => {
    mockFeed();
    const { unmount } = render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    unmount();

    expect(mockRemoveChannel).toHaveBeenCalledTimes(1);
    expect(mockRemoveChannel).toHaveBeenCalledWith(channelMock);
  });

  it("does not resubscribe when re-rendered with the same credential", async () => {
    mockFeed();
    const { rerender } = render(<ChatWindow credential={credential} />);
    await screen.findByText("live feed message");

    rerender(<ChatWindow credential={credential} />);

    expect(mockChannel).toHaveBeenCalledTimes(1);
    expect(channelMock?.subscribe).toHaveBeenCalledTimes(1);
  });
});
