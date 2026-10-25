import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockRpc = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    rpc: mockRpc,
  },
}));

import SearchView, {
  HighlightedSnippet,
  SEMANTIC_NOTICE,
} from "./SearchView";

const row = {
  id: "60000000-0000-0000-0000-000000000001",
  credential_id: "30000000-0000-0000-0000-000000000001",
  sender_key: "user_ab12cd34",
  sender_pseudonym: "user_9f8e7d6c",
  snippet: "Transfer <b>bitcoin</b> to the wallet",
  media_type: "text",
  created_at: new Date(Date.now() - 90 * 60_000).toISOString(),
};

function searchInput() {
  return screen.getByLabelText("Search evidence");
}

describe("SearchView", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    delete process.env.NEXT_PUBLIC_API_BASE;
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete process.env.NEXT_PUBLIC_API_BASE;
  });

  it("debounces typing into a single search_evidence call", async () => {
    mockRpc.mockResolvedValue({ data: [row], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.type(searchInput(), "bitcoin");

    await waitFor(
      () => {
        expect(mockRpc).toHaveBeenCalledTimes(1);
      },
      { timeout: 2000 },
    );
    expect(mockRpc).toHaveBeenCalledWith("search_evidence", {
      q: "bitcoin",
      credential_id: null,
      lim: 50,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows an FTS mode label on keyword results", async () => {
    mockRpc.mockResolvedValue({ data: [row], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.type(searchInput(), "bitcoin");

    const mode = await screen.findByTestId("result-mode", undefined, {
      timeout: 2000,
    });
    expect(mode).toHaveTextContent("FTS");
    expect(
      screen.getByRole("button", { name: /semantic search/i }),
    ).toHaveAttribute("aria-pressed", "false");
  });

  it("calls the semantic route when the toggle is on", async () => {
    process.env.NEXT_PUBLIC_API_BASE = "http://api.test";
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        results: [
          {
            message_id: row.id,
            credential_id: row.credential_id,
            distance: 0.12,
          },
        ],
        fallback: false,
      }),
    });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: /semantic search/i }),
    );
    await userEvent.type(searchInput(), "bitcoin");

    await waitFor(
      () => {
        expect(fetchMock).toHaveBeenCalledTimes(1);
      },
      { timeout: 2000 },
    );
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/monitor/semantic-search");
    expect(JSON.parse(init.body)).toEqual({
      q: "bitcoin",
      credential_id: null,
      limit: 50,
    });
    expect(mockRpc).not.toHaveBeenCalled();
    expect(await screen.findByTestId("result-mode")).toHaveTextContent(
      "Semantic",
    );
  });

  it("falls back to FTS with a notice when the semantic route fails", async () => {
    process.env.NEXT_PUBLIC_API_BASE = "http://api.test";
    fetchMock.mockRejectedValue(new Error("network down"));
    mockRpc.mockResolvedValue({ data: [row], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: /semantic search/i }),
    );
    await userEvent.type(searchInput(), "bitcoin");

    expect(
      await screen.findByText(SEMANTIC_NOTICE, undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledWith("search_evidence", {
      q: "bitcoin",
      credential_id: null,
      lim: 50,
    });
    expect(
      await screen.findByRole("button", { name: /open chat/i }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("result-mode")).toHaveTextContent("FTS");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("treats a fallback payload as semantic unavailable", async () => {
    process.env.NEXT_PUBLIC_API_BASE = "http://api.test";
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ results: [], fallback: true, reason: "embed_unreachable" }),
    });
    mockRpc.mockResolvedValue({ data: [row], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: /semantic search/i }),
    );
    await userEvent.type(searchInput(), "bitcoin");

    expect(
      await screen.findByText(SEMANTIC_NOTICE, undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledTimes(1);
  });

  it("keeps keyword search when no API base is configured", async () => {
    mockRpc.mockResolvedValue({ data: [row], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: /semantic search/i }),
    );
    await userEvent.type(searchInput(), "bitcoin");

    expect(
      await screen.findByText(SEMANTIC_NOTICE, undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(mockRpc).toHaveBeenCalledTimes(1);
  });

  it("renders result metadata and jumps to the chat", async () => {
    mockRpc.mockResolvedValue({ data: [row], error: null });
    const onOpenChat = vi.fn();
    render(<SearchView onOpenChat={onOpenChat} />);

    await userEvent.type(searchInput(), "bitcoin");

    const openChat = await screen.findByRole(
      "button",
      { name: /open chat/i },
      { timeout: 2000 },
    );
    expect(screen.getByTitle("user_ab12cd34")).toHaveTextContent("AB");
    expect(screen.getByText("bitcoin").tagName).toBe("MARK");
    expect(screen.getByText("text")).toBeInTheDocument();
    expect(screen.getByText(/ago|just now/)).toBeInTheDocument();
    expect(
      screen.getByText(/1 match for “bitcoin”/),
    ).toBeInTheDocument();

    await userEvent.click(openChat);
    expect(onOpenChat).toHaveBeenCalledWith(row.credential_id);
  });

  it("shows an explicit empty state", async () => {
    mockRpc.mockResolvedValue({ data: [], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.type(searchInput(), "zzzz-no-hits");

    expect(
      await screen.findByText("No matches", undefined, { timeout: 2000 }),
    ).toBeInTheDocument();
    expect(screen.getByText(/0 matches/)).toBeInTheDocument();
  });

  it("surfaces an RPC error with a working Retry button", async () => {
    mockRpc
      .mockResolvedValueOnce({ data: null, error: { message: "relation missing" } })
      .mockResolvedValueOnce({ data: [row], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.type(searchInput(), "bitcoin");

    const alert = await screen.findByRole(
      "alert",
      undefined,
      { timeout: 2000 },
    );
    expect(alert).toHaveTextContent("Search failed: relation missing");
    expect(screen.queryByText("No matches")).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /retry/i }));

    expect(
      await screen.findByRole(
        "button",
        { name: /open chat/i },
        { timeout: 2000 },
      ),
    ).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders hostile snippet markup as inert text", () => {
    const { container } = render(
      <HighlightedSnippet
        snippet={
          'Pay <b>bitcoin</b> <img src=x onerror="alert(1)"> <script>alert(2)</script>'
        }
      />,
    );

    expect(screen.getByText("bitcoin").tagName).toBe("MARK");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain('<img src=x onerror="alert(1)">');
    expect(container.textContent).toContain("<script>alert(2)</script>");
  });

  it("focuses the search input when `/` is pressed", async () => {
    mockRpc.mockResolvedValue({ data: [], error: null });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.keyboard("/");

    expect(searchInput()).toHaveFocus();
  });
});
