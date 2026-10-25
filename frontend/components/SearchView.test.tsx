import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockRpc = vi.hoisted(() => vi.fn());
const mockIn = vi.hoisted(() => vi.fn());
const mockSelect = vi.hoisted(() => vi.fn());
const mockFrom = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    rpc: mockRpc,
    from: mockFrom,
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
    mockFrom.mockReturnValue({ select: mockSelect });
    mockSelect.mockReturnValue({ in: mockIn });
    mockIn.mockResolvedValue({ data: [], error: null });
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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

  it("calls the same-origin proxy without any monitor key and hydrates rows", async () => {
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
    mockIn.mockResolvedValue({
      data: [
        {
          id: row.id,
          credential_id: row.credential_id,
          sender_key: row.sender_key,
          sender_pseudonym: row.sender_pseudonym,
          media_type: row.media_type,
          created_at: row.created_at,
          content: "please transfer bitcoin to the wallet now",
        },
      ],
      error: null,
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
    expect(url).toBe("/api/semantic-search");
    expect(JSON.parse(init.body)).toEqual({
      q: "bitcoin",
      credential_id: null,
      limit: 50,
    });
    expect(init.headers).not.toHaveProperty("X-Monitor-Key");
    expect(JSON.stringify(init.headers ?? {})).not.toMatch(
      /monitor_api_key|x-monitor-key/i,
    );
    expect(mockFrom).toHaveBeenCalledWith("evidence_redacted");
    expect(mockSelect).toHaveBeenCalledWith(
      "id, credential_id, sender_key, sender_pseudonym, media_type, created_at, content",
    );
    expect(mockIn).toHaveBeenCalledWith("id", [row.id]);
    expect(mockRpc).not.toHaveBeenCalled();
    expect(await screen.findByTestId("result-mode")).toHaveTextContent(
      "Semantic",
    );
    expect(screen.getByText("bitcoin").tagName).toBe("MARK");
    expect(screen.getByText(row.sender_key)).toBeInTheDocument();
    expect(screen.getByText(row.media_type)).toBeInTheDocument();
    expect(screen.getByText(/ago|just now/)).toBeInTheDocument();
  });

  it("falls back to FTS with a notice when the semantic proxy fails", async () => {
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

  it("treats a proxy fallback payload as semantic unavailable", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        results: [],
        fallback: true,
        reason: "semantic proxy not configured",
      }),
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
    expect(screen.getByTestId("result-mode")).toHaveTextContent("FTS");
  });

  it("falls back to FTS when hydration returns an error", async () => {
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
    mockIn.mockResolvedValue({ data: null, error: { message: "denied" } });
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
    expect(screen.getByTestId("result-mode")).toHaveTextContent("FTS");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("drops semantic hits that are missing from evidence_redacted", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({
        results: [
          { message_id: row.id, credential_id: row.credential_id, distance: 0.1 },
          {
            message_id: "60000000-0000-0000-0000-000000000099",
            credential_id: null,
            distance: 0.2,
          },
        ],
        fallback: false,
      }),
    });
    mockIn.mockResolvedValue({
      data: [
        {
          id: row.id,
          credential_id: row.credential_id,
          sender_key: row.sender_key,
          sender_pseudonym: row.sender_pseudonym,
          media_type: row.media_type,
          created_at: row.created_at,
          content: "bitcoin transfer instructions",
        },
      ],
      error: null,
    });
    render(<SearchView onOpenChat={vi.fn()} />);

    await userEvent.click(
      screen.getByRole("button", { name: /semantic search/i }),
    );
    await userEvent.type(searchInput(), "bitcoin");

    expect(
      await screen.findByText(/1 match \(semantic\) for “bitcoin”/, undefined, {
        timeout: 2000,
      }),
    ).toBeInTheDocument();
    expect(screen.getByTestId("result-mode")).toHaveTextContent("Semantic");
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
