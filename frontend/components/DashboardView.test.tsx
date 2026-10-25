import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const mockFrom = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: { from: mockFrom },
}));

import DashboardView, {
  EMPTY_MESSAGE,
  rangeCutoff,
  type CaptureRow,
  type HealthRow,
  type TakeoverRow,
  type VolumeRow,
} from "./DashboardView";

type QueryResult = {
  data: unknown[] | null;
  error: { message: string } | null;
};

const EMPTY_RESULT: QueryResult = { data: [], error: null };

const CAPTURES: CaptureRow[] = [
  { day: "2026-10-20", source: "shodan", credentials: 3, messages: 10 },
  { day: "2026-10-21", source: "github", credentials: 2, messages: 5 },
  { day: "2026-10-21", source: "shodan", credentials: 1, messages: 2 },
];

const VOLUMES: VolumeRow[] = [
  { day: "2026-10-20", media_type: "text", count: 10 },
  { day: "2026-10-21", media_type: "text", count: 7 },
];

const TAKEOVERS: TakeoverRow[] = [
  { day: "2026-10-20", attempted: 4, succeeded: 3, rate: 0.75 },
  { day: "2026-10-21", attempted: 0, succeeded: 0, rate: null },
];

const HEALTH: HealthRow[] = [
  { status: "active", count: 5, last_validated_day: "2026-10-21" },
  { status: "revoked", count: 1, last_validated_day: "2026-10-19" },
];

const FIXTURE: Record<string, QueryResult> = {
  metrics_capture_daily: { data: CAPTURES, error: null },
  metrics_message_volume_daily: { data: VOLUMES, error: null },
  metrics_takeover_daily: { data: TAKEOVERS, error: null },
  metrics_bot_health: { data: HEALTH, error: null },
};

let results: Record<string, QueryResult> = {};
let gteCalls: Array<{ table: string; column: string; value: string }> = [];

function installMock() {
  mockFrom.mockImplementation((table: string) => ({
    select: vi.fn(() => {
      const result = results[table] ?? EMPTY_RESULT;
      return {
        gte: vi.fn((column: string, value: string) => {
          gteCalls.push({ table, column, value });
          return Promise.resolve(result);
        }),
        then: (
          onFulfilled?: (value: QueryResult) => unknown,
          onRejected?: (reason: unknown) => unknown,
        ) => Promise.resolve(result).then(onFulfilled, onRejected),
      };
    }),
  }));
}

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  }));
});

beforeEach(() => {
  mockFrom.mockReset();
  results = {};
  gteCalls = [];
  installMock();
});

describe("DashboardView", () => {
  it("renders one labelled chart for each metrics view when data is present", async () => {
    results = FIXTURE;

    render(<DashboardView />);

    expect(
      await screen.findByRole("figure", { name: "Captures and messages over time" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: "Captures by source" })).toBeInTheDocument();
    expect(screen.getByRole("figure", { name: "Bot health by status" })).toBeInTheDocument();

    const takeover = screen.getByRole("figure", { name: "Takeover rate over time" });
    expect(takeover).toBeInTheDocument();
    expect(within(takeover).getByText("4")).toBeInTheDocument();
    expect(within(takeover).getByText("3")).toBeInTheDocument();
    expect(within(takeover).getByText("75%")).toBeInTheDocument();

    expect(screen.getAllByRole("figure")).toHaveLength(4);
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/Last 30 days/);
  });

  it("shows a per-chart empty state and renders nothing chart-like for [] data", async () => {
    results = {
      metrics_capture_daily: { data: [], error: null },
      metrics_message_volume_daily: { data: [], error: null },
      metrics_takeover_daily: { data: [], error: null },
      metrics_bot_health: { data: [], error: null },
    };

    render(<DashboardView />);

    expect(await screen.findAllByText(EMPTY_MESSAGE)).toHaveLength(4);
    expect(screen.getAllByRole("figure")).toHaveLength(4);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/0 days with activity/);
  });

  it("shows an error with retry and reloads on click", async () => {
    results = {
      ...FIXTURE,
      metrics_capture_daily: { data: null, error: { message: "boom" } },
    };

    render(<DashboardView />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Could not load metrics: boom/);

    results = {
      metrics_capture_daily: { data: [], error: null },
      metrics_message_volume_daily: { data: [], error: null },
      metrics_takeover_daily: { data: [], error: null },
      metrics_bot_health: { data: [], error: null },
    };

    await userEvent.click(screen.getByRole("button", { name: /retry/i }));

    await waitFor(() => {
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    });
    expect(screen.getAllByText(EMPTY_MESSAGE)).toHaveLength(4);
  });

  it("re-queries with the cutoff for the newly selected range", async () => {
    results = {
      metrics_capture_daily: { data: [], error: null },
      metrics_message_volume_daily: { data: [], error: null },
      metrics_takeover_daily: { data: [], error: null },
      metrics_bot_health: { data: [], error: null },
    };

    render(<DashboardView />);
    await screen.findAllByText(EMPTY_MESSAGE);

    // Default 30-day window: three day-filtered views, one cutoff each.
    expect(gteCalls).toHaveLength(3);
    expect(gteCalls.every((call) => call.column === "day")).toBe(true);
    expect(gteCalls.every((call) => call.value === rangeCutoff(30))).toBe(true);

    gteCalls = [];
    const ninety = screen.getByRole("button", { name: "90 days" });
    await userEvent.click(ninety);

    await waitFor(() => {
      expect(gteCalls.length).toBe(3);
    });
    expect(gteCalls.every((call) => call.value === rangeCutoff(90))).toBe(true);
    expect(ninety).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "30 days" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });
});
