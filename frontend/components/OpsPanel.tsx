"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Pause, Play, RefreshCw, TriangleAlert } from "lucide-react";

import { authHeaders } from "@/lib/api-auth";

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
};

const QUEUE_LABELS: Array<{ key: keyof OpsQueueLengths; label: string }> = [
  { key: "celery", label: "Celery" },
  { key: "scrape", label: "Scrape" },
  { key: "scanners", label: "Scanners" },
  { key: "validation", label: "Validation" },
];

type PendingAction = "pause" | "resume" | null;

export default function OpsPanel() {
  const [status, setStatus] = useState<OpsStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingAction>(null);
  const [confirmingPause, setConfirmingPause] = useState(false);

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
      const body = (await response.json().catch(() => null)) as OpsStatus | null;
      if (!response.ok || !body) {
        throw new Error("status unavailable");
      }
      if (!mountedRef.current) return true;
      setStatus(body);
      setLoadError(null);
      return true;
    } catch {
      if (mountedRef.current) {
        setLoadError("System status is unavailable.");
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
      } | null;
      if (!response.ok) {
        throw new Error(
          typeof body?.error === "string"
            ? body.error
            : `Could not ${action} operations.`,
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

  const busy = pending !== null;

  return (
    <section
      aria-label="Operations controls"
      className="h-full overflow-y-auto bg-[#f4f7fa]"
    >
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-[#f4f7fa]/95 px-4 py-4 backdrop-blur md:px-7">
        <p className="font-mono text-[11px] uppercase tracking-[0.24em] text-[#197e74]">
          Runtime control
        </p>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <h1 className="font-[Arial_Narrow,Arial,sans-serif] text-3xl font-black tracking-tight text-[#172033]">
              Operations
            </h1>
            <p className="mt-1 text-sm text-slate-600">
              Pause or resume scanners, broadcasters and validators — the same
              runtime switch the bot commands use.
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              void refresh();
            }}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] disabled:cursor-not-allowed disabled:opacity-50"
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
              className="rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
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
                <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
                  System state
                </p>
                <p className="mt-2 flex items-center gap-2 text-2xl font-black text-[#172033]">
                  <span
                    aria-hidden="true"
                    className={`inline-block h-2.5 w-2.5 rounded-full ${
                      status.paused ? "bg-amber-500" : "bg-[#197e74]"
                    }`}
                  />
                  <span aria-label="System state">
                    {status.paused ? "Paused" : "Running"}
                  </span>
                </p>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
                  Redis
                </p>
                <p
                  aria-label="Redis health"
                  className={`mt-2 text-2xl font-black ${
                    status.redis === "ok" ? "text-[#197e74]" : "text-red-700"
                  }`}
                >
                  {status.redis === "ok" ? "Online" : "Unreachable"}
                </p>
              </div>

              <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                <p className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
                  Pending broadcasts
                </p>
                <p
                  aria-label="Pending broadcasts"
                  className="mt-2 text-2xl font-black text-[#172033]"
                >
                  {status.pending_broadcasts ?? "—"}
                </p>
              </div>
            </div>

            <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
              <h2 className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
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
                      className="font-mono text-lg font-bold text-[#172033]"
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
                  className="flex items-center gap-2 rounded-md bg-[#172033] px-3 py-2 text-sm font-bold text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] disabled:cursor-not-allowed disabled:opacity-50"
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
                  className="flex items-center gap-2 rounded-md border border-[#197e74] bg-white px-3 py-2 text-sm font-bold text-[#197e74] hover:bg-[#197e74]/10 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] disabled:cursor-not-allowed disabled:opacity-50"
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
                  className="mt-4 rounded-md border border-amber-200 bg-amber-50 p-3"
                >
                  <p className="text-sm font-semibold text-amber-900">
                    Pause all scanners and broadcasting?
                  </p>
                  <p className="mt-1 text-xs text-amber-800">
                    In-flight tasks finish; new work waits until you resume.
                  </p>
                  <div className="mt-3 flex gap-2">
                    <button
                      type="button"
                      onClick={() => {
                        void applyAction("pause");
                      }}
                      disabled={busy}
                      className="rounded-md border border-amber-400 bg-amber-100 px-3 py-1.5 text-xs font-bold text-amber-900 hover:bg-amber-200 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Confirm pause
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmingPause(false)}
                      disabled={busy}
                      className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      Cancel
                    </button>
                  </div>
                </div>
              )}

              <p
                role="status"
                aria-live="polite"
                className="mt-3 min-h-4 text-sm font-semibold text-[#197e74]"
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
          </>
        )}
      </div>
    </section>
  );
}
