import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mockFrom = vi.hoisted(() => vi.fn());
const mockRpc = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: {
    from: mockFrom,
    rpc: mockRpc,
  },
}));

import FindingsQueue from "./FindingsQueue";

const finding = {
  id: "20000000-0000-0000-0000-000000000001",
  type: "credential_exposure",
  canonical_key: "credential:30000000-0000-0000-0000-000000000001",
  title: "Active Telegram credential exposure",
  summary: "Credential 30000000 remains active; source=github.",
  why_it_matters: "An active third-party credential can permit unauthorized access.",
  recommended_action: "Verify ownership, then rotate or revoke the credential.",
  confidence: 0.9,
  severity: "high",
  priority: 9,
  score_explanation: {
    confidence: {
      value: 0.9,
      contributors: [{ name: "validated_active" }],
    },
    severity: { value: "high", contributors: [{ name: "active_credential" }] },
    priority: { value: 9, contributors: [{ name: "severity" }] },
  },
  status: "new",
  assignee: null,
  first_seen_at: "2026-09-04T00:00:00Z",
  last_seen_at: "2026-09-04T01:00:00Z",
  evidence_count: 1,
  material_version: 2,
  last_material_change_at: "2026-09-04T01:00:00Z",
};

const evidence = {
  id: "40000000-0000-0000-0000-000000000001",
  finding_id: finding.id,
  evidence_type: "credential_record",
  source_table: "discovered_credentials",
  source_id: "30000000-0000-0000-0000-000000000001",
  observed_at: "2026-09-04T01:00:00Z",
  weight: 1,
  excerpt_redacted: "status=active; source=github",
  provenance: { producer: "credential_exposure_v1" },
};

function builderFor(data: unknown[], error: { message: string } | null = null) {
  const builder = {
    select: vi.fn(),
    eq: vi.fn(),
    in: vi.fn(),
    order: vi.fn(),
    limit: vi.fn(),
  };
  builder.select.mockReturnValue(builder);
  builder.eq.mockReturnValue(builder);
  builder.in.mockReturnValue(builder);
  builder.order.mockReturnValue(builder);
  builder.limit.mockResolvedValue({ data, error });
  return builder;
}

describe("FindingsQueue", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState({}, "", "/");
    mockFrom.mockImplementation((table: string) => {
      if (table === "findings") return builderFor([finding]);
      if (table === "finding_evidence") return builderFor([evidence]);
      if (table === "exfiltrated_messages") return builderFor([]);
      throw new Error(`Unexpected table ${table}`);
    });
    mockRpc.mockResolvedValue({ data: "feedback-id", error: null });
  });

  it("loads priority-first findings and expands redacted evidence", async () => {
    render(<FindingsQueue onDrilldown={vi.fn()} />);

    const title = await screen.findByText("Active Telegram credential exposure");
    expect(screen.getByText("1 shown · 0 critical · 1 unassigned")).toBeInTheDocument();
    expect(screen.getByLabelText("Priority 9 of 10").children).toHaveLength(10);

    await userEvent.click(title.closest("button")!);

    expect(await screen.findByText("status=active; source=github")).toBeInTheDocument();
    expect(screen.getByText(/validated_active/)).toBeInTheDocument();
    expect(screen.queryByText(/bot token/i)).not.toBeInTheDocument();
  });

  it("records one-click feedback and updates the visible disposition", async () => {
    render(<FindingsQueue onDrilldown={vi.fn()} />);
    const title = await screen.findByText("Active Telegram credential exposure");
    await userEvent.click(title.closest("button")!);
    await userEvent.click(await screen.findByRole("button", { name: "Useful" }));

    await waitFor(() => {
      expect(mockRpc).toHaveBeenCalledWith("record_finding_feedback", {
        p_finding_id: finding.id,
        p_label: "useful",
        p_reason_code: "actionable",
        p_note: null,
        p_status: "triaged",
        p_assignee: null,
        p_suppress_pattern: null,
      });
    });
    expect(await screen.findByText("Marked useful and triaged.")).toBeInTheDocument();
  });

  it("opens Chat as a secondary drill-down for linked credential evidence", async () => {
    const onDrilldown = vi.fn();
    render(<FindingsQueue onDrilldown={onDrilldown} />);
    const title = await screen.findByText("Active Telegram credential exposure");
    await userEvent.click(title.closest("button")!);
    await userEvent.click(await screen.findByRole("button", { name: "Chat" }));

    expect(onDrilldown).toHaveBeenCalledWith(
      "30000000-0000-0000-0000-000000000001",
      "chat",
    );
  });

  it("opens digest deep links with evidence already loaded", async () => {
    window.history.replaceState({}, "", `/?finding=${finding.id}`);
    render(<FindingsQueue onDrilldown={vi.fn()} />);

    expect(await screen.findByText("status=active; source=github")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Telemetry" })).toBeInTheDocument();
  });

  it("turns query failures into an actionable queue state", async () => {
    mockFrom.mockImplementation(() => builderFor([], { message: "relation missing" }));
    render(<FindingsQueue onDrilldown={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Findings could not be loaded: relation missing",
    );
    expect(screen.getByRole("alert")).toHaveTextContent("applied");
  });
});
