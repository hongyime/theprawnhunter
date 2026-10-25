import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockFrom = vi.hoisted(() => vi.fn());
const mockUseAuth = vi.hoisted(() => vi.fn());
const mockRpc = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: { from: mockFrom, rpc: mockRpc },
}));

vi.mock("@/lib/auth", () => ({
  useAuth: mockUseAuth,
}));

import type { Credential } from "@/app/page";
import ChatWindow from "./ChatWindow";

const credential = {
  id: "30000000-0000-0000-0000-000000000001",
  source: "test",
  created_at: "2026-09-05T00:00:00Z",
  meta: {},
} as Credential;

const liveMessage = {
  id: "message-live",
  credential_id: credential.id,
  sender_pseudonym: "user_live",
  content: "live feed message",
  media_type: "text",
  is_broadcasted: false,
  created_at: "2026-09-05T02:00:00Z",
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

function messageBuilder(result: { data: unknown[]; error: unknown }) {
  const builder = {
    select: vi.fn(),
    eq: vi.fn(),
    order: vi.fn(),
  };
  builder.select.mockReturnValue(builder);
  builder.eq.mockReturnValue(builder);
  builder.order.mockResolvedValue(result);
  return builder;
}

function mockFeed(messages: unknown[] = [liveMessage]) {
  mockFrom.mockReturnValue(messageBuilder({ data: messages, error: null }));
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
  });

  it("does not query evidence without an authenticated session", async () => {
    mockUseAuth.mockReturnValue({ session: null });
    render(<ChatWindow credential={credential} />);

    expect(await screen.findByText("Sign in required to view exfiltrated messages.")).toBeInTheDocument();
    expect(mockFrom).not.toHaveBeenCalled();
  });

  it("fetches the newest 200 messages then renders them chronologically", async () => {
    mockUseAuth.mockReturnValue({ session: { user: { id: "operator" } } });
    const builder = messageBuilder({
      data: [
        {
          id: "new",
          credential_id: credential.id,
          sender_pseudonym: "user_new",
          content: "newest message",
          media_type: "text",
          is_broadcasted: false,
          created_at: "2026-09-05T02:00:00Z",
        },
        {
          id: "old",
          credential_id: credential.id,
          sender_pseudonym: "user_old",
          content: "older message",
          media_type: "text",
          is_broadcasted: false,
          created_at: "2026-09-05T01:00:00Z",
        },
      ],
      error: null,
    });
    mockFrom.mockReturnValue(builder);
    render(<ChatWindow credential={credential} />);

    await screen.findByText("newest message");
    expect(mockFrom).toHaveBeenCalledWith("evidence_redacted");
    expect(builder.order).toHaveBeenCalledWith("created_at", { ascending: false });
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
});
