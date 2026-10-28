"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ExternalLink,
  Loader2,
  RefreshCw,
  ShieldAlert,
  TriangleAlert,
} from "lucide-react";

import { authHeaders } from "@/lib/api-auth";
import { proxyFailureMessage } from "@/lib/proxy-errors";
import { supabase } from "@/lib/supabase";

export type WatchlistHit = {
  hit_id: string;
  rule_id: string;
  rule_name: string | null;
  message_id: string;
  credential_id: string | null;
  matched_text: string | null;
  telegram_msg_id: number | null;
  created_at: string | null;
};

export const HIT_LIMITS = [25, 50, 100] as const;
export const DEFAULT_HIT_LIMIT = 50;
export const MATCHED_TEXT_MAX_LENGTH = 120;
export const WATCHLIST_HIT_LINK_ROUTE = "/api/watchlist-hit-link";
export const DELETED_RULE_LABEL = "deleted rule";
export const ALL_RULES = "__all__";

/**
 * Render time as a compact relative label with the absolute timestamp on hover.
 * Invalid or missing timestamps degrade to a neutral label instead of throwing.
 */
export function formatHitTime(value: string | null): {
  label: string;
  title: string;
} {
  if (!value) return { label: "Unknown time", title: "Unknown time" };
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return { label: "Unknown time", title: "Unknown time" };
  }
  const title = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
  const minutes = Math.round((Date.now() - date.getTime()) / 60_000);
  if (minutes < 1) return { label: "just now", title };
  if (minutes < 60) return { label: `${minutes}m ago`, title };
  const hours = Math.round(minutes / 60);
  if (hours < 24) return { label: `${hours}h ago`, title };
  const days = Math.round(hours / 24);
  if (days < 30) return { label: `${days}d ago`, title };
  return { label: title, title };
}

/** Truncate a match for the table while keeping the full value in the title. */
export function truncateMatchedText(value: string | null): string {
  if (!value) return "—";
  // The string is rendered as a React text node: React escapes it, so hostile
  // markup (e.g. `<script>`) is shown literally rather than interpreted.
  return value.length > MATCHED_TEXT_MAX_LENGTH
    ? `${value.slice(0, MATCHED_TEXT_MAX_LENGTH)}…`
    : value;
}

function shortCredential(value: string | null): string {
  return value ? `${value.slice(0, 8)}…` : "global";
}

export default function WatchlistHitsView() {
  const [hits, setHits] = useState<WatchlistHit[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const [limit, setLimit] = useState<number>(DEFAULT_HIT_LIMIT);
  const [ruleFilter, setRuleFilter] = useState<string>(ALL_RULES);
  const [openingId, setOpeningId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function loadHits() {
      setLoading(true);
      setLoadError(null);
      const { data, error } = await supabase.rpc("list_watchlist_hits", {
        lim: limit,
      });
      if (!active) return;
      if (error) {
        setHits([]);
        setLoadError(`Watchlist hits could not be loaded: ${error.message}`);
      } else {
        setHits((data as WatchlistHit[] | null) ?? []);
      }
      setLoading(false);
    }

    loadHits();
    return () => {
      active = false;
    };
  }, [limit, retryToken]);

  const ruleNames = useMemo(() => {
    const names = new Set<string>();
    for (const hit of hits) {
      names.add(hit.rule_name || DELETED_RULE_LABEL);
    }
    return Array.from(names).sort((a, b) => a.localeCompare(b));
  }, [hits]);

  const visibleHits = useMemo(() => {
    if (ruleFilter === ALL_RULES) return hits;
    return hits.filter(
      (hit) => (hit.rule_name || DELETED_RULE_LABEL) === ruleFilter,
    );
  }, [hits, ruleFilter]);

  // Reset the filter when a reload no longer contains the selected rule.
  useEffect(() => {
    if (ruleFilter !== ALL_RULES && !ruleNames.includes(ruleFilter)) {
      setRuleFilter(ALL_RULES);
    }
  }, [ruleFilter, ruleNames]);

  async function openInTelegram(hit: WatchlistHit) {
    setLinkError(null);
    setOpeningId(hit.hit_id);
    try {
      const response = await fetch(
        `${WATCHLIST_HIT_LINK_ROUTE}?hit_id=${encodeURIComponent(hit.hit_id)}`,
        {
          method: "GET",
          cache: "no-store",
          headers: await authHeaders(),
        },
      );
      const payload = (await response.json().catch(() => null)) as {
        url?: unknown;
        code?: unknown;
      } | null;
      if (!response.ok) {
        if (response.status === 404) {
          setLinkError("No Telegram link is available for this hit.");
        } else {
          setLinkError(
            proxyFailureMessage(
              typeof payload?.code === "string" ? payload.code : null,
              response.status,
              "Telegram link lookup",
            ),
          );
        }
        return;
      }
      if (typeof payload?.url === "string" && payload.url.length > 0) {
        window.open(payload.url, "_blank", "noopener,noreferrer");
      } else if (typeof payload?.code === "string") {
        setLinkError(
          proxyFailureMessage(
            payload.code,
            response.status,
            "Telegram link lookup",
          ),
        );
      } else {
        setLinkError("No Telegram link is available for this hit.");
      }
    } catch {
      setLinkError(proxyFailureMessage(null, 0, "Telegram link lookup"));
    } finally {
      setOpeningId(null);
    }
  }

  return (
    <section
      className="h-full overflow-y-auto bg-paper"
      aria-label="Watchlist hits"
    >
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-paper/95 px-4 py-4 backdrop-blur md:px-7">
        <div className="flex flex-wrap items-end justify-end gap-3">
          <h1 className="sr-only">Watchlist hits</h1>

          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label
                htmlFor="watchlist-hit-rule-filter"
                className="block font-mono text-xs uppercase tracking-[0.16em] text-slate-500"
              >
                Rule
              </label>
              <select
                id="watchlist-hit-rule-filter"
                value={ruleFilter}
                onChange={(event) => setRuleFilter(event.target.value)}
                className="mt-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-800 shadow-sm focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent/30"
              >
                <option value={ALL_RULES}>All rules</option>
                {ruleNames.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </div>

            <div>
              <label
                htmlFor="watchlist-hit-limit"
                className="block font-mono text-xs uppercase tracking-[0.16em] text-slate-500"
              >
                Limit
              </label>
              <select
                id="watchlist-hit-limit"
                value={limit}
                onChange={(event) => setLimit(Number(event.target.value))}
                className="mt-1 rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-800 shadow-sm focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent/30"
              >
                {HIT_LIMITS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </div>
      </div>

      <div className="mx-auto flex max-w-6xl flex-col gap-3 p-4 md:p-7">
        {linkError && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-300 bg-slate-100 p-4 text-sm text-slate-700"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{linkError}</span>
          </div>
        )}

        {loading && (
          <div
            className="space-y-2"
            aria-hidden="true"
            data-testid="watchlist-hits-loading"
          >
            {[0, 1, 2, 3].map((index) => (
              <div
                key={index}
                className="grid grid-cols-[0.8fr_1fr_0.7fr_1.6fr_0.8fr] gap-4 rounded-lg border border-slate-200 bg-white p-4 motion-safe:animate-pulse"
              >
                <div className="h-3 w-16 rounded bg-slate-200" />
                <div className="h-3 w-24 rounded bg-slate-200" />
                <div className="h-3 w-14 rounded bg-slate-200" />
                <div className="h-3 w-full rounded bg-slate-200" />
                <div className="h-3 w-20 rounded bg-slate-200" />
              </div>
            ))}
          </div>
        )}

        {!loading && loadError && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{loadError}</span>
            <button
              type="button"
              onClick={() => setRetryToken((value) => value + 1)}
              className="flex items-center gap-1.5 rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              <RefreshCw className="h-3.5 w-3.5" /> Retry
            </button>
          </div>
        )}

        {!loading && !loadError && hits.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
            <ShieldAlert
              aria-hidden="true"
              className="mx-auto h-7 w-7 text-accent"
            />
            <h2 className="mt-3 font-bold text-slate-900">
              No watchlist hits yet
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              Matches appear here after a rule is evaluated against captured
              messages.
            </p>
          </div>
        )}

        {!loading && !loadError && hits.length > 0 && visibleHits.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center text-sm text-slate-600">
            No hits recorded for this rule.
          </div>
        )}

        {!loading && !loadError && visibleHits.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-[0_1px_0_rgba(23,32,51,0.04)]">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="bg-slate-50 font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                  <tr>
                    <th scope="col" className="px-4 py-3">
                      Time
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Rule
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Credential
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Matched text
                    </th>
                    <th scope="col" className="px-4 py-3 text-right">
                      Action
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {visibleHits.map((hit) => {
                    const time = formatHitTime(hit.created_at);
                    const matched = truncateMatchedText(hit.matched_text);
                    const opening = openingId === hit.hit_id;
                    return (
                      <tr key={hit.hit_id} className="align-top">
                        <td className="whitespace-nowrap px-4 py-3">
                          <span
                            className="font-mono text-xs text-slate-600"
                            title={time.title}
                          >
                            {time.label}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          {hit.rule_name ? (
                            <span className="font-semibold text-slate-800">
                              {hit.rule_name}
                            </span>
                          ) : (
                            <span className="text-slate-500">
                              {DELETED_RULE_LABEL}{" "}
                              <span className="font-mono text-xs text-slate-400">
                                {hit.rule_id.slice(0, 8)}
                              </span>
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <span
                            className="font-mono text-xs text-slate-600"
                            title={hit.credential_id ?? "Global rule"}
                          >
                            {shortCredential(hit.credential_id)}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <span
                            className="block max-w-[32rem] break-words text-sm text-slate-700"
                            title={
                              hit.matched_text
                                ? hit.matched_text
                                : "No matched text"
                            }
                          >
                            {matched}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex justify-end">
                            <button
                              type="button"
                              disabled={opening}
                              onClick={() => openInTelegram(hit)}
                              className="flex items-center gap-1.5 rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-bold text-slate-700 hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                            >
                              {opening ? (
                                <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
                              ) : (
                                <ExternalLink className="h-3.5 w-3.5" />
                              )}
                              Open in Telegram
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </section>
  );
}
