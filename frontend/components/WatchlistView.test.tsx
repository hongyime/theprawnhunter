import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockRpc = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    rpc: mockRpc,
  },
}));

import WatchlistView, { type WatchlistRule } from "./WatchlistView";

const literalRule: WatchlistRule = {
  id: "70000000-0000-4000-8000-000000000001",
  name: "Wallet address mentions",
  pattern: "bc1q",
  is_regex: false,
  credential_id: null,
  enabled: true,
  cooldown_minutes: 60,
  created_at: "2026-10-25T00:00:00Z",
  updated_at: "2026-10-25T00:00:00Z",
  created_by: null,
};

const regexRule: WatchlistRule = {
  id: "70000000-0000-4000-8000-000000000002",
  name: "Rug pull chatter",
  pattern: "\\brug ?pull\\b",
  is_regex: true,
  credential_id: "30000000-0000-4000-8000-000000000001",
  enabled: false,
  cooldown_minutes: 15,
  created_at: "2026-10-24T00:00:00Z",
  updated_at: "2026-10-24T00:00:00Z",
  created_by: null,
};

type RpcResult = {
  data: unknown;
  error: { message: string } | null;
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function listResult(rules: WatchlistRule[]): RpcResult {
  return { data: rules, error: null };
}

function mockList(rules: WatchlistRule[]) {
  mockRpc.mockImplementation((fn: string) => {
    if (fn === "list_watchlist_rules") return Promise.resolve(listResult(rules));
    return Promise.resolve({ data: null, error: null });
  });
}

async function openCreateDialog() {
  await userEvent.click(screen.getByRole("button", { name: /new rule/i }));
  return screen.getByRole("dialog");
}

async function fillRequiredFields(name: string, pattern: string) {
  await userEvent.type(screen.getByLabelText("Name"), name);
  await userEvent.type(screen.getByLabelText("Pattern"), pattern);
}

describe("WatchlistView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockList([]);
  });

  it("renders the rule list with scope, cooldown, and a keyboard toggle", async () => {
    mockList([literalRule, regexRule]);
    render(<WatchlistView />);

    expect(
      await screen.findByText(literalRule.name),
    ).toBeInTheDocument();
    expect(screen.getByText(regexRule.name)).toBeInTheDocument();
    expect(screen.getByText("Global")).toBeInTheDocument();
    expect(screen.getByText("15 min")).toBeInTheDocument();
    expect(screen.getByText("60 min")).toBeInTheDocument();

    const enabled = screen.getByRole("switch", {
      name: `Disable ${literalRule.name}`,
    });
    expect(enabled).toHaveAttribute("aria-checked", "true");

    const disabled = screen.getByRole("switch", {
      name: `Enable ${regexRule.name}`,
    });
    expect(disabled).toHaveAttribute("aria-checked", "false");
    expect(mockRpc).toHaveBeenCalledWith("list_watchlist_rules");
  });

  it("marks regex rules with a badge and keeps patterns monospace", async () => {
    mockList([regexRule, literalRule]);
    render(<WatchlistView />);

    await screen.findByText(regexRule.name);

    expect(screen.getAllByText("REGEX")).toHaveLength(1);
    expect(screen.getAllByText("TEXT")).toHaveLength(1);
    expect(screen.getByText(regexRule.pattern).tagName).toBe("CODE");
  });

  it("shows the create row optimistically before the RPC resolves", async () => {
    const pending = deferred<RpcResult>();
    mockRpc.mockImplementation((fn: string) => {
      if (fn === "list_watchlist_rules") {
        return Promise.resolve(listResult([]));
      }
      if (fn === "upsert_watchlist_rule") return pending.promise;
      throw new Error(`Unexpected RPC ${fn}`);
    });

    render(<WatchlistView />);
    expect(
      await screen.findByText("No watchlist rules yet"),
    ).toBeInTheDocument();

    await openCreateDialog();
    await fillRequiredFields("Crypto wallet mentions", "bc1q");
    await userEvent.click(
      screen.getByRole("button", { name: /create rule/i }),
    );

    // The optimistic row is visible while the request is still in flight.
    expect(
      await screen.findByText("Crypto wallet mentions"),
    ).toBeInTheDocument();
    expect(mockRpc).toHaveBeenCalledWith("upsert_watchlist_rule", {
      p_id: null,
      p_name: "Crypto wallet mentions",
      p_pattern: "bc1q",
      p_is_regex: false,
      p_credential_id: null,
      p_enabled: true,
      p_cooldown_minutes: 60,
    });

    const savedRule: WatchlistRule = {
      ...literalRule,
      id: "70000000-0000-4000-8000-000000000099",
      name: "Crypto wallet mentions",
      pattern: "bc1q",
    };
    pending.resolve({ data: savedRule, error: null });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByText("Crypto wallet mentions")).toBeInTheDocument();
  });

  it("edits an existing rule and sends its id to the upsert RPC", async () => {
    const updated: WatchlistRule = {
      ...literalRule,
      name: "Renamed wallet rule",
    };
    mockRpc.mockImplementation((fn: string) => {
      if (fn === "list_watchlist_rules") {
        return Promise.resolve(listResult([literalRule]));
      }
      if (fn === "upsert_watchlist_rule") {
        return Promise.resolve({ data: updated, error: null });
      }
      throw new Error(`Unexpected RPC ${fn}`);
    });

    render(<WatchlistView />);
    await screen.findByText(literalRule.name);

    await userEvent.click(
      screen.getByRole("button", { name: `Edit ${literalRule.name}` }),
    );
    const nameInput = screen.getByLabelText("Name");
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, "Renamed wallet rule");
    await userEvent.click(
      screen.getByRole("button", { name: /save changes/i }),
    );

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(mockRpc).toHaveBeenCalledWith("upsert_watchlist_rule", {
      p_id: literalRule.id,
      p_name: "Renamed wallet rule",
      p_pattern: literalRule.pattern,
      p_is_regex: literalRule.is_regex,
      p_credential_id: null,
      p_enabled: true,
      p_cooldown_minutes: 60,
    });
    expect(await screen.findByText("Renamed wallet rule")).toBeInTheDocument();
    expect(screen.queryByText(literalRule.name)).not.toBeInTheDocument();
  });

  it("rolls back the optimistic create and shows an inline error", async () => {
    mockRpc.mockImplementation((fn: string) => {
      if (fn === "list_watchlist_rules") {
        return Promise.resolve(listResult([]));
      }
      if (fn === "upsert_watchlist_rule") {
        return Promise.resolve({
          data: null,
          error: { message: "Operator authorization required" },
        });
      }
      throw new Error(`Unexpected RPC ${fn}`);
    });

    render(<WatchlistView />);
    await screen.findByText("No watchlist rules yet");

    await openCreateDialog();
    await fillRequiredFields("Crypto wallet mentions", "bc1q");
    await userEvent.click(
      screen.getByRole("button", { name: /create rule/i }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Rule could not be saved: Operator authorization required",
    );
    expect(
      screen.queryByText("Crypto wallet mentions"),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toHaveValue(
      "Crypto wallet mentions",
    );
  });

  it("confirms deletes and restores the row when the RPC fails", async () => {
    mockList([literalRule]);
    mockRpc.mockImplementation((fn: string) => {
      if (fn === "list_watchlist_rules") {
        return Promise.resolve(listResult([literalRule]));
      }
      if (fn === "delete_watchlist_rule") {
        return Promise.resolve({
          data: null,
          error: { message: "Permission denied" },
        });
      }
      throw new Error(`Unexpected RPC ${fn}`);
    });

    render(<WatchlistView />);
    await screen.findByText(literalRule.name);

    await userEvent.click(
      screen.getByRole("button", {
        name: `Delete rule ${literalRule.name}`,
      }),
    );
    const confirm = await screen.findByRole("button", {
      name: `Confirm delete ${literalRule.name}`,
    });
    await userEvent.click(confirm);

    expect(mockRpc).toHaveBeenCalledWith("delete_watchlist_rule", {
      p_id: literalRule.id,
    });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      `Rule “${literalRule.name}” could not be deleted: Permission denied`,
    );
    expect(
      await screen.findByText(literalRule.name),
    ).toBeInTheDocument();
  });

  it("blocks submission for oversized patterns and cooldowns below one", async () => {
    render(<WatchlistView />);
    await screen.findByText("No watchlist rules yet");

    await openCreateDialog();
    const pattern = screen.getByLabelText("Pattern");
    const submit = screen.getByRole("button", { name: /create rule/i });

    fireEvent.change(pattern, { target: { value: "a".repeat(201) } });
    expect(submit).toBeDisabled();
    expect(
      screen.getByText("Pattern must be 200 characters or fewer."),
    ).toBeInTheDocument();

    fireEvent.change(pattern, { target: { value: "wallet" } });
    expect(submit).not.toBeDisabled();

    fireEvent.change(screen.getByLabelText("Cooldown (minutes)"), {
      target: { value: "0" },
    });
    expect(submit).toBeDisabled();
    expect(
      screen.getByText("Cooldown must be at least 1 minute."),
    ).toBeInTheDocument();
    expect(mockRpc).not.toHaveBeenCalledWith(
      "upsert_watchlist_rule",
      expect.anything(),
    );
  });
});
