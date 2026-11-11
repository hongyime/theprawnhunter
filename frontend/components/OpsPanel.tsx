"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play, RefreshCw, TriangleAlert } from "lucide-react";

import { authHeaders } from "@/lib/api-auth";
import { proxyFailureMessage } from "@/lib/proxy-errors";

export const OPS_POLL_INTERVAL_MS = 30_000;

export type OpsQueueLengths = {
  celery: number;
  scrape: number;
  scanners: number;
  validation: number;
};

export type OpsStatus = {
  paused: boolean;
  redis: "ok" | "unreachable";
  pending_broadcasts: number | null;
  queue_lengths: OpsQueueLengths;
  bot_count: number | null;
  circuit_breakers?: CircuitBreakers;
  delivery?: DeliveryMetrics;
};

type DeliveryMetrics = {
  monitorable_topics: number | null;
  polled_24h_topics: number | null;
  stale_topics: number | null;
  no_chat_credentials: number | null;
  active_message_topics: number | null;
  pending_messages: number | null;
  oldest_pending_age_seconds: number | null;
  selected_last_run: number | null;
  delivered_last_run: number | null;
  eligible_anonymous_senders: number | null;
  coverage_24h_percent: number | null;
  coverage_slo_percent: number;
};

const QUEUE_LABELS: Array<{ key: keyof OpsQueueLengths; label: string }> = [
  { key: "celery", label: "Celery" },
  { key: "scrape", label: "Scrape" },
  { key: "scanners", label: "Scanners" },
  { key: "validation", label: "Validation" },
];

type PendingAction = "pause" | "resume" | null;
type CircuitBreakers = Record<string, { state: string; failure_count: number }>;

export default function OpsPanel() {
  const [status, setStatus] = useState<OpsStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [confirmingPause, setConfirmingPause] = useState(false);
  const [circuitBreakers, setCircuitBreakers] = useState<CircuitBreakers>({});
  const [scanSource, setScanSource] = useState("shodan");
  const [scanQuery, setScanQuery] = useState("");

  const pendingRef = useRef(false);
  const mountedRef = useRef(true);

  const refresh = useCallback(async (): Promise<boolean> => {
    // A poll must never clobber an in-flight optimistic change.
    if (pendingRef.current) return true;
    try {
      const headers = await authHeaders();
      const response = await fetch("/api/ops/status", {
        cache: "no-store",
        headers,
      });
      const body = (await response.json().catch(() => null)) as
        | (OpsStatus & { code?: unknown })
        | null;
      if (!response.ok || !body) {
        if (mountedRef.current) {
          setLoadError(
            proxyFailureMessage(
              typeof body?.code === "string" ? body.code : null,
              response.status,
              "Operations status",
            ),
          );
        }
        return false;
      }
      if (!mountedRef.current) return true;
      setStatus(body);
      setLoadError(null);
      setCircuitBreakers(body.circuit_breakers ?? {});
      return true;
    } catch {
      if (mountedRef.current) {
        setLoadError(proxyFailureMessage(null, 0, "Operations status"));
      }
      return false;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    let stopped = false;
    let timer: number | null = null;

    const tick = () => {
      if (stopped || document.hidden) return;
      void refresh();
    };

    const start = () => {
      if (timer === null) {
        timer = window.setInterval(tick, OPS_POLL_INTERVAL_MS);
      }
    };

    const stop = () => {
      if (timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    };

    const handleVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        tick();
        start();
      }
    };

    tick();
    start();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      stopped = true;
      mountedRef.current = false;
      stop();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [refresh]);

  async function applyAction(action: "pause" | "resume") {
    if (pendingRef.current) return;

    const previous = status;
    pendingRef.current = true;
    setPending(action);
    setActionError(null);
    setActionMessage(null);
    setStatus((current) =>
      current ? { ...current, paused: action === "pause" } : current,
    );

    try {
      const response = await fetch(`/api/ops/${action}`, {
        method: "POST",
        cache: "no-store",
        headers: await authHeaders(),
      });
      const body = (await response.json().catch(() => null)) as {
        paused?: unknown;
        error?: unknown;
        code?: unknown;
      } | null;
      if (!response.ok) {
        throw new Error(
          proxyFailureMessage(
            typeof body?.code === "string" ? body.code : null,
            response.status,
            "Operations control",
          ),
        );
      }

      const paused =
        typeof body?.paused === "boolean" ? body.paused : action === "pause";
      setStatus((current) => (current ? { ...current, paused } : current));
      setActionMessage(action === "pause" ? "System paused." : "System resumed.");
      setConfirmingPause(false);
    } catch (error) {
      // Roll back the optimistic flip; the server remains the source of truth.
      setStatus(previous);
      setActionError(
        error instanceof Error
          ? error.message
          : "The operation could not be applied.",
      );
    } finally {
      pendingRef.current = false;
      setPending(null);
    }
  }

  async function applyUtility(action: "retry-failed-broadcasts" | "reset-circuit-breaker" | "scan", body?: Record<string, string>) {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setActionError(null);
    setActionMessage(null);
    try {
      const response = await fetch(`/api/ops/${action}`, {
        method: "POST", cache: "no-store", headers: { ...(await authHeaders()), ...(body ? { "Content-Type": "application/json" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      const result = (await response.json().catch(() => null)) as { code?: unknown } | null;
      if (!response.ok) throw new Error(proxyFailureMessage(typeof result?.code === "string" ? result.code : null, response.status, "Operations control"));
      setActionMessage(action === "scan" ? "Scan queued." : action === "retry-failed-broadcasts" ? "Failed broadcasts queued for retry." : "Circuit breaker reset.");
      if (action !== "scan") await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "The operation could not be applied.");
    } finally {
      pendingRef.current = false;
    }
  }

  const busy = pending !== null;

  return (
    <section
      aria-label="Operations controls"
      className="h-full overflow-y-auto bg-paper"
    >
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-paper/95 px-4 py-4 backdrop-blur md:px-7">
        <div className="flex flex-wrap items-center justify-end gap-3">
          <h1 className="sr-only">Operations</h1>
          <button
            type="button"
            onClick={() => {
              void refresh();
            }}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
          >
            <RefreshCw className="h-3.5 w-3.5" /> Refresh
          </button>
        </div>
      </div>

      <div className="mx-auto flex max-w-5xl flex-col gap-4 p-4 md:p-7">
        {loadError && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{loadError}</span>
            <button
              type="button"
              onClick={() => {
                void refresh();
              }}
              className="rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              Retry
            </button>
          </div>
        )}

        {!status && !loadError && (
          <div
            className="space-y-3 motion-safe:animate-pulse"
            aria-hidden="true"
          >
            <div className="h-24 rounded-lg border border-slate-200 bg-white" />
            <div className="h-32 rounded-lg border border-slate-200 bg-white" />
          </div>
        )}

        {status && (
          <>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                <p className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                  System state
                </p>
                <p className="mt-2 flex items-center gap-2 text-2xl font-bold text-ink">
                  <span
                    aria-hidden="true"
                    className={`inline-block h-2.5 w-2.5 rounded-full ${
                      status.paused ? "bg-red-600" : "bg-accent"
                    }`}
                  />
                  <span aria-label="System state">
                    {status.paused ? "Paused" : "Running"}
                  </span>
                </p>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                <p className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                  Redis
                </p>
                <p
                  aria-label="Redis health"
                  className={`mt-2 text-2xl font-bold ${
                    status.redis === "ok" ? "text-accent" : "text-red-700"
                  }`}
                >
                  {status.redis === "ok" ? "Online" : "Unreachable"}
                </p>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                <p className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                  Pending broadcasts
                </p>
                <p
                  aria-label="Pending broadcasts"
                  className="mt-2 text-2xl font-bold text-ink"
                >
                  {status.pending_broadcasts ?? "—"}
                </p>
              </div>
            </div>

            {status.delivery && (
              <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                <h2 className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                  Delivery coverage (24h SLO: {status.delivery.coverage_slo_percent}%)
                </h2>
                <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {[
                    ["Coverage", status.delivery.coverage_24h_percent === null ? "—" : `${status.delivery.coverage_24h_percent}%`],
                    ["Topics polled", `${status.delivery.polled_24h_topics ?? "—"}/${status.delivery.monitorable_topics ?? "—"}`],
                    ["Stale / no chat", `${status.delivery.stale_topics ?? "—"}/${status.delivery.no_chat_credentials ?? "—"}`],
                    ["Active message topics", status.delivery.active_message_topics ?? "—"],
                    ["Oldest pending", status.delivery.oldest_pending_age_seconds === null ? "—" : `${Math.floor(status.delivery.oldest_pending_age_seconds / 3600)}h`],
                    ["Selected / delivered", `${status.delivery.selected_last_run ?? "—"}/${status.delivery.delivered_last_run ?? "—"}`],
                    ["Eligible senders", status.delivery.eligible_anonymous_senders ?? "—"],
                  ].map(([label, value]) => (
                    <div key={String(label)} className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2">
                      <dt className="text-xs font-semibold text-slate-600">{label}</dt>
                      <dd className="font-mono text-lg font-bold text-ink">{value}</dd>
                    </div>
                  ))}
                </dl>
              </div>
            )}

            <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
              <h2 className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                Queue lengths
              </h2>
              <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                {QUEUE_LABELS.map(({ key, label }) => (
                  <div
                    key={key}
                    className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2"
                  >
                    <dt className="text-xs font-semibold text-slate-600">
                      {label}
                    </dt>
                    <dd
                      aria-label={`Queue ${key}`}
                      className="font-mono text-lg font-bold text-ink"
                    >
                      {status.queue_lengths[key] ?? 0}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>

            <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
              <div className="flex flex-wrap items-center gap-2">
                <button
                  type="button"
                  onClick={() => setConfirmingPause(true)}
                  disabled={busy || status.paused || confirmingPause}
                  className="flex items-center gap-2 rounded-md bg-ink px-3 py-2 text-sm font-bold text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Pause aria-hidden="true" className="h-4 w-4" />
                  Pause
                </button>
                <button
                  type="button"
                  onClick={() => {
                    void applyAction("resume");
                  }}
                  disabled={busy || !status.paused}
                  className="flex items-center gap-2 rounded-md border border-accent bg-white px-3 py-2 text-sm font-bold text-accent hover:bg-accent/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Play aria-hidden="true" className="h-4 w-4" />
                  Resume
                </button>
                {busy && (
                  <span className="font-mono text-xs text-slate-500">
                    Applying…
                  </span>
                )}
              </div>

              {confirmingPause && (
                <div
                  role="group"
                  aria-label="Confirm pause"
                  className="mt-4 rounded-md border border-red-200 bg-red-50 p-3"
                >
                  <p className="text-sm font-semibold text-red-800">
                    Pause all scanners and broadcasting?
                  </p>
                  <p className="mt-1 text-xs text-red-700">
                    In-flight tasks finish; new work waits until you resume.
                  </p>
                  <div className="mt-3 flex gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        void applyAction("pause");
                      }}
                      disabled={busy}
                      className="rounded-md border border-red-300 bg-red-100 px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Confirm pause
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmingPause(false)}
                      disabled={busy}
                      className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              <p
                role="status"
                aria-live="polite"
                className="mt-3 min-h-4 text-sm font-semibold text-accent"
              >
                {actionMessage}
              </p>
              {actionError && (
                <p
                  role="alert"
                  aria-live="assertive"
                  className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
                >
                  {actionError}
                </p>
              )}
            </div>

            <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
              <h2 className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">Circuit breakers</h2>
              <div className="mt-3 space-y-2">
                {Object.entries(circuitBreakers).map(([service, breaker]) => (
                  <div key={service} className="flex items-center gap-3 text-sm"><span className="min-w-24 font-medium">{service}</span><span>{breaker.state} ({breaker.failure_count} failures)</span><button type="button" disabled={busy} onClick={() => void applyUtility("reset-circuit-breaker", { service })} className="rounded border border-slate-300 px-2 py-1 text-xs font-bold">Reset</button></div>
                ))}
              </div>
              <button type="button" disabled={busy} onClick={() => void applyUtility("retry-failed-broadcasts")} className="mt-4 rounded border border-slate-300 px-3 py-2 text-sm font-bold disabled:opacity-50">Retry failed broadcasts</button>
            </div>

            <form className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]" onSubmit={(event) => { event.preventDefault(); void applyUtility("scan", { source: scanSource, query: scanQuery }); }}>
              <h2 className="font-mono text-xs uppercase tracking-[0.16em] text-slate-500">Manual scan</h2>
              <div className="mt-3 flex flex-wrap gap-2"><select aria-label="Scan source" value={scanSource} onChange={(event) => setScanSource(event.target.value)} className="rounded border border-slate-300 p-2 text-sm">{["shodan", "fofa", "github", "urlscan", "sourcegraph", "searchcode"].map((source) => <option key={source}>{source}</option>)}</select><input aria-label="Scan query" value={scanQuery} onChange={(event) => setScanQuery(event.target.value)} className="min-w-56 flex-1 rounded border border-slate-300 p-2 text-sm" /><button type="submit" disabled={busy || !scanQuery.trim()} className="rounded bg-ink px-3 py-2 text-sm font-bold text-white disabled:opacity-50">Queue scan</button></div>
            </form>
          </>
        )}
      </div>
    </section>
  );
}
