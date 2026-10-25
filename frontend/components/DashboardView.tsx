"use client";

import { useEffect, useMemo, useState } from "react";
import { RefreshCw, TriangleAlert } from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { supabase } from "@/lib/supabase";

export const RANGE_OPTIONS = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
] as const;

export type RangeDays = (typeof RANGE_OPTIONS)[number]["days"];

export const EMPTY_MESSAGE = "No data for this range";

/**
 * Explicit chart ink. Recharts' defaults are bright primaries that clash with
 * the dashboard's dark ink + teal palette; every stroke/fill below is spelled
 * out so light and dark surfaces both keep contrast.
 */
export const CHART_COLORS = {
  captures: "#0d9488",
  messages: "#475569",
  source: "#0d9488",
  rate: "#7c3aed",
  grid: "#cbd5e1",
  axis: "#64748b",
  tooltipBackground: "#0f172a",
  tooltipText: "#f8fafc",
} as const;

const HEALTH_COLORS: Record<string, string> = {
  active: "#0d9488",
  pending: "#d97706",
  revoked: "#dc2626",
  unknown: "#64748b",
};

export type CaptureRow = {
  day: string | null;
  source: string | null;
  credentials: number | string | null;
  messages: number | string | null;
};

export type VolumeRow = {
  day: string | null;
  media_type: string | null;
  count: number | string | null;
};

export type TakeoverRow = {
  day: string | null;
  attempted: number | string | null;
  succeeded: number | string | null;
  rate: number | string | null;
};

export type HealthRow = {
  status: string | null;
  count: number | string | null;
  last_validated_day: string | null;
};

type Metrics = {
  captures: CaptureRow[];
  volumes: VolumeRow[];
  takeovers: TakeoverRow[];
  health: HealthRow[];
};

/** Inclusive lower bound for a metrics query: today (UTC) minus `days`. */
export function rangeCutoff(days: RangeDays, now: Date = new Date()): string {
  const cutoff = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()),
  );
  cutoff.setUTCDate(cutoff.getUTCDate() - days);
  return cutoff.toISOString().slice(0, 10);
}

function toNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function dayKey(value: string | null): string {
  return typeof value === "string" ? value.slice(0, 10) : "";
}

const TICK_MULTIPLIERS = [1, 2, 5, 10] as const;

/**
 * Pick a "nice" integer step (1, 2 or 5 × 10ⁿ) so the axis needs at most
 * `maxIntervals` intervals. Steps below 1 are clamped to 1 because every axis
 * in this view counts whole things (captures, messages, rate percent).
 */
function niceStep(max: number, maxIntervals: number): number {
  const rough = max / maxIntervals;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  for (const multiplier of TICK_MULTIPLIERS) {
    const step = multiplier * magnitude;
    if (step >= 1 && Math.ceil(max / step) <= maxIntervals) return step;
  }
  return Math.max(1, Math.ceil(rough));
}

/**
 * Explicit integer Y-axis ticks for a non-negative maximum. Recharts' auto
 * ticks can emit fractional values that `allowDecimals={false}` then filters,
 * leaving duplicate labels (e.g. `100, 100, 100`). Supplying our own unique
 * integers avoids that and keeps the axis sane for empty/all-zero data.
 *
 * The result always starts at 0, ascends, contains no duplicates, and its last
 * value is >= `max`.
 */
export function niceIntegerTicks(max: number, maxIntervals = 5): number[] {
  if (!Number.isFinite(max) || max <= 0) return [0, 1];

  const step = niceStep(max, Math.max(1, Math.floor(maxIntervals)));
  const top = Math.ceil(max / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= top; value += step) ticks.push(value);
  return ticks;
}

export function buildTrendSeries(captures: CaptureRow[], volumes: VolumeRow[]) {
  const byDay = new Map<string, { day: string; captures: number; messages: number }>();

  for (const row of captures) {
    const day = dayKey(row.day);
    if (!day) continue;
    const entry = byDay.get(day) ?? { day, captures: 0, messages: 0 };
    entry.captures += toNumber(row.credentials);
    byDay.set(day, entry);
  }

  for (const row of volumes) {
    const day = dayKey(row.day);
    if (!day) continue;
    const entry = byDay.get(day) ?? { day, captures: 0, messages: 0 };
    entry.messages += toNumber(row.count);
    byDay.set(day, entry);
  }

  return [...byDay.values()].sort((a, b) => a.day.localeCompare(b.day));
}

export function buildSourceSeries(captures: CaptureRow[]) {
  const bySource = new Map<string, number>();
  for (const row of captures) {
    const source = row.source || "unknown";
    bySource.set(source, (bySource.get(source) ?? 0) + toNumber(row.credentials));
  }
  return [...bySource.entries()]
    .map(([source, credentials]) => ({ source, credentials }))
    .filter((row) => row.credentials > 0)
    .sort((a, b) => b.credentials - a.credentials)
    .slice(0, 8);
}

export function buildHealthSeries(health: HealthRow[]) {
  return health
    .map((row) => ({
      status: row.status || "unknown",
      count: toNumber(row.count),
      lastValidatedDay: row.last_validated_day,
    }))
    .filter((row) => row.count > 0)
    .sort((a, b) => b.count - a.count);
}

export function buildTakeoverSeries(takeovers: TakeoverRow[]) {
  return takeovers
    .map((row) => {
      const day = dayKey(row.day);
      const rawRate = row.rate;
      return {
        day,
        attempted: toNumber(row.attempted),
        succeeded: toNumber(row.succeeded),
        ratePct:
          rawRate === null || rawRate === undefined || rawRate === ""
            ? null
            : Math.round(toNumber(rawRate) * 1000) / 10,
      };
    })
    .filter((row) => row.day !== "")
    .sort((a, b) => a.day.localeCompare(b.day));
}

export function takeoverTotals(series: ReturnType<typeof buildTakeoverSeries>) {
  let attempted = 0;
  let succeeded = 0;
  for (const row of series) {
    attempted += row.attempted;
    succeeded += row.succeeded;
  }
  return {
    attempted,
    succeeded,
    ratePct: attempted > 0 ? Math.round((succeeded / attempted) * 1000) / 10 : null,
  };
}

const tooltipProps = {
  contentStyle: {
    background: CHART_COLORS.tooltipBackground,
    border: `1px solid ${CHART_COLORS.grid}`,
    borderRadius: 6,
    fontSize: 12,
    color: CHART_COLORS.tooltipText,
  },
  labelStyle: { color: CHART_COLORS.tooltipText, fontWeight: 600 },
  itemStyle: { color: CHART_COLORS.tooltipText },
} as const;

const axisTick = { fontSize: 11, fill: CHART_COLORS.axis } as const;

function ChartCard({
  title,
  caption,
  label,
  hasData,
  children,
}: {
  title: string;
  caption: string;
  label: string;
  hasData: boolean;
  children: React.ReactNode;
}) {
  return (
    <figure
      aria-label={label}
      className="min-w-0 rounded-lg border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900"
    >
      <figcaption className="font-mono text-[11px] uppercase tracking-[0.16em] text-[#197e74] dark:text-teal-400">
        {title}
      </figcaption>
      <p className="mt-1 mb-3 text-sm text-slate-600 dark:text-slate-400">{caption}</p>
      {hasData ? (
        children
      ) : (
        <p className="flex h-40 items-center justify-center rounded border border-dashed border-slate-300 text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400">
          {EMPTY_MESSAGE}
        </p>
      )}
    </figure>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded border border-slate-200 bg-slate-50 px-2 py-1.5 dark:border-slate-700 dark:bg-slate-800">
      <dt className="font-mono text-[10px] uppercase tracking-wide text-slate-500 dark:text-slate-400">
        {label}
      </dt>
      <dd className="text-lg font-bold text-[#172033] dark:text-slate-100">{value}</dd>
    </div>
  );
}

export default function DashboardView() {
  const [range, setRange] = useState<RangeDays>(30);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  useEffect(() => {
    let active = true;
    const cutoff = rangeCutoff(range);

    setLoading(true);
    setError(null);

    Promise.all([
      supabase
        .from("metrics_capture_daily")
        .select("day, source, credentials, messages")
        .gte("day", cutoff),
      supabase
        .from("metrics_message_volume_daily")
        .select("day, media_type, count")
        .gte("day", cutoff),
      supabase
        .from("metrics_takeover_daily")
        .select("day, attempted, succeeded, rate")
        .gte("day", cutoff),
      supabase.from("metrics_bot_health").select("status, count, last_validated_day"),
    ])
      .then(([captures, volumes, takeovers, health]) => {
        if (!active) return;
        const failed = [captures, volumes, takeovers, health].find(
          (result) => result.error,
        );
        if (failed?.error) {
          setError(failed.error.message || "Unknown error");
          setMetrics(null);
        } else {
          setMetrics({
            captures: (captures.data ?? []) as CaptureRow[],
            volumes: (volumes.data ?? []) as VolumeRow[],
            takeovers: (takeovers.data ?? []) as TakeoverRow[],
            health: (health.data ?? []) as HealthRow[],
          });
        }
        setLoading(false);
      })
      .catch((cause: unknown) => {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : "Unexpected error");
        setMetrics(null);
        setLoading(false);
      });

    return () => {
      active = false;
    };
  }, [range, retryToken]);

  const trend = useMemo(
    () => buildTrendSeries(metrics?.captures ?? [], metrics?.volumes ?? []),
    [metrics],
  );
  const sources = useMemo(() => buildSourceSeries(metrics?.captures ?? []), [metrics]);
  const health = useMemo(() => buildHealthSeries(metrics?.health ?? []), [metrics]);
  const takeover = useMemo(
    () => buildTakeoverSeries(metrics?.takeovers ?? []),
    [metrics],
  );
  const totals = useMemo(() => takeoverTotals(takeover), [takeover]);

  const trendMax = useMemo(
    () => trend.reduce((peak, row) => Math.max(peak, row.captures, row.messages), 0),
    [trend],
  );
  const trendTicks = useMemo(() => niceIntegerTicks(trendMax), [trendMax]);

  const sourceMax = useMemo(
    () => sources.reduce((peak, row) => Math.max(peak, row.credentials), 0),
    [sources],
  );
  const sourceTicks = useMemo(() => niceIntegerTicks(sourceMax), [sourceMax]);

  const rateMax = useMemo(
    () => takeover.reduce((peak, row) => Math.max(peak, row.ratePct ?? 0), 0),
    [takeover],
  );
  const rateTicks = useMemo(() => niceIntegerTicks(rateMax), [rateMax]);

  const summary = loading
    ? "Loading metrics…"
    : error
      ? "Metrics unavailable"
      : `Last ${range} days · ${trend.length} ${trend.length === 1 ? "day" : "days"} with activity`;

  return (
    <section
      aria-label="Dashboard charts"
      aria-busy={loading}
      className="h-full overflow-y-auto bg-[#f4f7fa] dark:bg-slate-950"
    >
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-[#f4f7fa]/95 px-4 py-4 backdrop-blur md:px-7 dark:border-slate-800 dark:bg-slate-950/95">
        <p className="font-mono text-[11px] uppercase tracking-[0.24em] text-[#197e74] dark:text-teal-400">
          Operational trends
        </p>
        <div className="mt-1 flex flex-wrap items-end justify-between gap-3">
          <h1 className="font-[Arial_Narrow,Arial,sans-serif] text-3xl font-black tracking-tight text-[#172033] dark:text-slate-100">
            Dashboard
          </h1>
          <div
            role="group"
            aria-label="Chart date range"
            className="grid grid-cols-3 gap-1 rounded-md bg-slate-100 p-1 dark:bg-slate-800"
          >
            {RANGE_OPTIONS.map((option) => {
              const selected = range === option.days;
              return (
                <button
                  key={option.days}
                  type="button"
                  onClick={() => setRange(option.days)}
                  aria-pressed={selected}
                  className={`rounded px-3 py-1.5 text-xs font-semibold transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] ${
                    selected
                      ? "bg-white text-slate-900 shadow-sm dark:bg-slate-700 dark:text-white"
                      : "text-slate-500 hover:text-slate-800 dark:text-slate-400 dark:hover:text-slate-200"
                  }`}
                >
                  {option.label}
                </button>
              );
            })}
          </div>
        </div>
        <p
          role="status"
          aria-live="polite"
          className="mt-2 text-sm text-slate-600 dark:text-slate-400"
        >
          {summary}
        </p>
      </div>

      <div className="mx-auto flex max-w-6xl flex-col gap-4 p-4 md:p-7">
        {loading && (
          <div className="grid gap-4 md:grid-cols-2" aria-hidden="true">
            {[0, 1, 2, 3].map((index) => (
              <div
                key={index}
                className="animate-pulse rounded-lg border border-slate-200 bg-white p-4 motion-reduce:animate-none dark:border-slate-800 dark:bg-slate-900"
              >
                <div className="h-3 w-24 rounded bg-slate-200 dark:bg-slate-700" />
                <div className="mt-3 h-40 rounded bg-slate-100 dark:bg-slate-800" />
              </div>
            ))}
          </div>
        )}

        {!loading && error && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">Could not load metrics: {error}</span>
            <button
              type="button"
              onClick={() => setRetryToken((value) => value + 1)}
              className="flex items-center gap-1.5 rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] dark:border-red-700 dark:bg-red-900 dark:text-red-100"
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Retry
            </button>
          </div>
        )}

        {!loading && !error && metrics && (
          <div className="grid gap-4 md:grid-cols-2">
            <ChartCard
              title="Capture & message trend"
              caption="Daily credential captures and captured messages across the selected range."
              label="Captures and messages over time"
              hasData={trend.length > 0}
            >
              <div className="h-56 w-full" data-y-ticks={trendTicks.join(",")}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={trend} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                    <CartesianGrid
                      stroke={CHART_COLORS.grid}
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="day"
                      tick={axisTick}
                      tickFormatter={(value: string) => value.slice(5)}
                    />
                    <YAxis
                      allowDecimals={false}
                      domain={[0, trendTicks[trendTicks.length - 1]]}
                      ticks={trendTicks}
                      tick={axisTick}
                      width={40}
                    />
                    <Tooltip {...tooltipProps} />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Line
                      type="monotone"
                      dataKey="captures"
                      name="Captures"
                      stroke={CHART_COLORS.captures}
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="messages"
                      name="Messages"
                      stroke={CHART_COLORS.messages}
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>

            <ChartCard
              title="Captures by source"
              caption="Credential captures per discovery source (top eight)."
              label="Captures by source"
              hasData={sources.length > 0}
            >
              <div className="h-56 w-full" data-y-ticks={sourceTicks.join(",")}>
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={sources} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                    <CartesianGrid
                      stroke={CHART_COLORS.grid}
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="source"
                      tick={axisTick}
                      tickFormatter={(value: string) =>
                        value.length > 9 ? `${value.slice(0, 8)}…` : value
                      }
                    />
                    <YAxis
                      allowDecimals={false}
                      domain={[0, sourceTicks[sourceTicks.length - 1]]}
                      ticks={sourceTicks}
                      tick={axisTick}
                      width={40}
                    />
                    <Tooltip {...tooltipProps} />
                    <Bar
                      dataKey="credentials"
                      name="Captures"
                      fill={CHART_COLORS.source}
                      radius={[3, 3, 0, 0]}
                      isAnimationActive={false}
                    />
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>

            <ChartCard
              title="Bot health"
              caption="Current credential status distribution."
              label="Bot health by status"
              hasData={health.length > 0}
            >
              <div className="h-56 w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <PieChart>
                    <Tooltip {...tooltipProps} />
                    <Legend wrapperStyle={{ fontSize: 12 }} />
                    <Pie
                      data={health}
                      dataKey="count"
                      nameKey="status"
                      innerRadius="55%"
                      outerRadius="85%"
                      paddingAngle={2}
                      stroke="#ffffff"
                      strokeWidth={2}
                      isAnimationActive={false}
                    >
                      {health.map((entry) => (
                        <Cell
                          key={entry.status}
                          fill={HEALTH_COLORS[entry.status] ?? HEALTH_COLORS.unknown}
                        />
                      ))}
                    </Pie>
                  </PieChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>

            <ChartCard
              title="Takeover funnel"
              caption="Webhook takeover attempts, successes and daily success rate."
              label="Takeover rate over time"
              hasData={takeover.length > 0}
            >
              <dl className="mb-3 grid grid-cols-3 gap-2">
                <Stat label="Attempted" value={totals.attempted} />
                <Stat label="Succeeded" value={totals.succeeded} />
                <Stat
                  label="Rate"
                  value={totals.ratePct === null ? "—" : `${totals.ratePct}%`}
                />
              </dl>
              <div className="h-40 w-full" data-y-ticks={rateTicks.join(",")}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={takeover}
                    margin={{ top: 8, right: 8, bottom: 0, left: -16 }}
                  >
                    <CartesianGrid
                      stroke={CHART_COLORS.grid}
                      strokeDasharray="3 3"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="day"
                      tick={axisTick}
                      tickFormatter={(value: string) => value.slice(5)}
                    />
                    <YAxis
                      domain={[0, rateTicks[rateTicks.length - 1]]}
                      ticks={rateTicks}
                      tick={axisTick}
                      width={40}
                      tickFormatter={(value: number) => `${value}%`}
                    />
                    <Tooltip {...tooltipProps} />
                    <Line
                      type="monotone"
                      dataKey="ratePct"
                      name="Takeover rate"
                      stroke={CHART_COLORS.rate}
                      strokeWidth={2}
                      dot={{ r: 2, fill: CHART_COLORS.rate }}
                      connectNulls
                      isAnimationActive={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </ChartCard>
          </div>
        )}
      </div>
    </section>
  );
}
