import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockFrom = vi.hoisted(() => vi.fn());
const mockUseAuth = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: { from: mockFrom },
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

function messageBuilder(result: { data: unknown[]; error: unknown }) {
  const builder = {
    select: vi.fn(),
    eq: vi.fn(),
    order: vi.fn(),
    limit: vi.fn(),
  };
  builder.select.mockReturnValue(builder);
  builder.eq.mockReturnValue(builder);
  builder.order.mockReturnValue(builder);
  builder.limit.mockResolvedValue(result);
  return builder;
}

describe("ChatWindow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
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
});
