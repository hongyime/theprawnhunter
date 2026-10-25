import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const mockFrom = vi.hoisted(() => vi.fn());

vi.mock("@/lib/supabase", () => ({
  supabase: { from: mockFrom },
}));

import DashboardView, {
  EMPTY_MESSAGE,
  niceIntegerTicks,
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

// Reproduces the live defect: a 140/day message peak plus 5 % takeover rates.
// Recharts' auto axis previously rounded fractional ticks to "100, 100, 100"
// and "0 %, 5 %, 5 %".
const BUG_FIXTURE: Record<string, QueryResult> = {
  metrics_capture_daily: {
    data: [
      { day: "2026-10-20", source: "shodan", credentials: 140, messages: 140 },
      { day: "2026-10-21", source: "github", credentials: 7, messages: 7 },
    ],
    error: null,
  },
  metrics_message_volume_daily: {
    data: [
      { day: "2026-10-20", media_type: "text", count: 140 },
      { day: "2026-10-21", media_type: "text", count: 7 },
    ],
    error: null,
  },
  metrics_takeover_daily: {
    data: [
      { day: "2026-10-20", attempted: 10, succeeded: 1, rate: 0.05 },
      { day: "2026-10-21", attempted: 20, succeeded: 1, rate: 0.05 },
    ],
    error: null,
  },
  metrics_bot_health: { data: HEALTH, error: null },
};

const ZERO_FIXTURE: Record<string, QueryResult> = {
  metrics_capture_daily: {
    data: [{ day: "2026-10-20", source: "shodan", credentials: 0, messages: 0 }],
    error: null,
  },
  metrics_message_volume_daily: {
    data: [{ day: "2026-10-20", media_type: "text", count: 0 }],
    error: null,
  },
  metrics_takeover_daily: {
    data: [{ day: "2026-10-20", attempted: 0, succeeded: 0, rate: null }],
    error: null,
  },
  metrics_bot_health: { data: [], error: null },
};

function axisTicks(figure: HTMLElement): number[] {
  const axis = figure.querySelector("[data-y-ticks]");
  if (!axis) throw new Error("chart is missing its explicit Y-axis ticks");
  return (axis.getAttribute("data-y-ticks") ?? "")
    .split(",")
    .filter((value) => value !== "")
    .map(Number);
}

function expectUniqueAscendingTicks(ticks: number[], minimum: number) {
  expect(ticks.length).toBeGreaterThanOrEqual(2);
  expect(new Set(ticks).size).toBe(ticks.length);
  expect(ticks[0]).toBe(0);
  expect(ticks[ticks.length - 1]).toBeGreaterThanOrEqual(minimum);
  ticks.forEach((tick, index) => {
    expect(Number.isInteger(tick)).toBe(true);
    if (index > 0) expect(tick).toBeGreaterThan(ticks[index - 1]);
  });
}

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

describe("niceIntegerTicks", () => {
  it("falls back to 0 and 1 for empty, zero, negative or non-finite maxima", () => {
    expect(niceIntegerTicks(0)).toEqual([0, 1]);
    expect(niceIntegerTicks(-5)).toEqual([0, 1]);
    expect(niceIntegerTicks(Number.NaN)).toEqual([0, 1]);
    expect(niceIntegerTicks(Number.POSITIVE_INFINITY)).toEqual([0, 1]);
  });

  it("returns at most six unique ascending integers that reach the maximum", () => {
    for (const max of [1, 3, 7, 10, 75, 140, 20831]) {
      const ticks = niceIntegerTicks(max);
      expect(ticks.length).toBeLessThanOrEqual(6);
      expectUniqueAscendingTicks(ticks, max);
    }
  });

  it("uses 1/2/5-style steps for the reported magnitudes", () => {
    expect(niceIntegerTicks(7)).toEqual([0, 2, 4, 6, 8]);
    expect(niceIntegerTicks(140)).toEqual([0, 50, 100, 150]);
    expect(niceIntegerTicks(20831)).toEqual([0, 5000, 10000, 15000, 20000, 25000]);
  });
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

  it("gives every numeric Y axis unique integer ticks for the bug dataset", async () => {
    results = BUG_FIXTURE;

    render(<DashboardView />);

    const trend = await screen.findByRole("figure", {
      name: "Captures and messages over time",
    });
    const trendTicks = axisTicks(trend);
    expectUniqueAscendingTicks(trendTicks, 140);
    expect(trendTicks).toEqual([0, 50, 100, 150]);

    const sources = screen.getByRole("figure", { name: "Captures by source" });
    const sourceTicks = axisTicks(sources);
    expectUniqueAscendingTicks(sourceTicks, 140);

    await screen.findByRole("figure", { name: "Takeover rate over time" });
    const rateTicks = axisTicks(screen.getByRole("figure", { name: "Takeover rate over time" }));
    expectUniqueAscendingTicks(rateTicks, 5);
  });

  it("keeps a sane 0..1 axis when every value is zero", async () => {
    results = ZERO_FIXTURE;

    render(<DashboardView />);

    const trend = await screen.findByRole("figure", {
      name: "Captures and messages over time",
    });
    expect(axisTicks(trend)).toEqual([0, 1]);

    const rate = screen.getByRole("figure", { name: "Takeover rate over time" });
    expect(axisTicks(rate)).toEqual([0, 1]);
  });
});
