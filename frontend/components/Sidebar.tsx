"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { supabase } from "@/lib/supabase";
import { BarChart3, BellRing, LayoutDashboard, ListChecks, MessageSquareText, LucideTarget, Radar, Search } from "lucide-react";
import type { Credential, DashboardView } from "@/app/page";

export const TARGET_FETCH_LIMIT = 2000;
export const SEARCH_DEBOUNCE_MS = 250;
/** Below this length the sidebar filters the loaded rows in the browser. */
export const SERVER_SEARCH_MIN_QUERY = 2;
/** RPC result cap; the migration clamps this to 1..200 as well. */
export const SERVER_SEARCH_LIMIT = 200;

export type SidebarCredential = Credential & { status?: string | null };

export const STATUS_FILTERS = ["active", "revoked", "pending", "unknown"] as const;
export const CHAT_TYPE_FILTERS = ["private", "group", "supergroup", "channel"] as const;

export type TargetFilters = {
  query: string;
  statuses: readonly string[];
  chatTypes: readonly string[];
};

export function credentialStatus(cred: SidebarCredential): string {
  const status = (cred.status ?? "").toLowerCase();
  return status === "active" || status === "revoked" || status === "pending"
    ? status
    : "unknown";
}

/** Case-insensitive match against bot username, chat title and chat name. */
export function matchesTargetQuery(cred: SidebarCredential, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const haystack = [cred.meta?.bot_username, cred.meta?.chat_title, cred.chat_name];
  return haystack.some(
    (value) => typeof value === "string" && value.toLowerCase().includes(needle),
  );
}

/** Keeps the first row per id so merged server/client results render once. */
export function dedupeTargetsById(
  targets: readonly SidebarCredential[],
): SidebarCredential[] {
  const seen = new Map<string, SidebarCredential>();
  for (const target of targets) {
    if (!seen.has(target.id)) seen.set(target.id, target);
  }
  return [...seen.values()];
}

/**
 * Text query AND status chips AND chat-type chips.
 * Chips within one group are OR'd; groups combine with AND.
 */
export function filterTargets(
  credentials: readonly SidebarCredential[],
  { query, statuses, chatTypes }: TargetFilters,
): SidebarCredential[] {
  return credentials.filter((cred) => {
    if (!matchesTargetQuery(cred, query)) return false;
    if (statuses.length > 0 && !statuses.includes(credentialStatus(cred))) return false;
    if (chatTypes.length > 0 && !chatTypes.includes((cred.chat_type ?? "").toLowerCase())) {
      return false;
    }
    return true;
  });
}

function ConfidenceBadge({ score }: { score: number | null }) {
  if (score === null || score === undefined) {
    return <span className="text-xs text-gray-400 font-mono">–</span>
  }
  const { label, cls } =
    score >= 0.8
      ? { label: 'High', cls: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' }
      : score >= 0.4
      ? { label: 'Med', cls: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200' }
      : { label: 'Low', cls: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' }
  return (
    <span className={`text-xs px-1.5 py-0.5 rounded font-medium ${cls}`}>
      {label}
    </span>
  )
}

function chatTypeLabel(chatType?: string | null): string {
  switch (chatType) {
    case "private":
      return "DM";
    case "channel":
      return "Channel";
    case "group":
    case "supergroup":
      return "Group";
    default:
      return "Chat";
  }
}

export default function Sidebar({
    selected,
    activeView,
    onViewChange,
    onSelect,
}: {
    selected: Credential | null;
    activeView: DashboardView;
    onViewChange: (view: DashboardView) => void;
    onSelect: (cred: Credential) => void;
}) {
    const [credentials, setCredentials] = useState<SidebarCredential[]>([]);
    const [loading, setLoading] = useState(true);
    const [query, setQuery] = useState("");
    const [debouncedQuery, setDebouncedQuery] = useState("");
    const [activeStatuses, setActiveStatuses] = useState<string[]>([]);
    const [activeChatTypes, setActiveChatTypes] = useState<string[]>([]);
    const [serverTargets, setServerTargets] = useState<SidebarCredential[] | null>(null);
    const [serverSearchError, setServerSearchError] = useState<string | null>(null);
    const filterInputRef = useRef<HTMLInputElement>(null);

    useEffect(() => {
        function handleKeyDown(event: KeyboardEvent) {
            if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
            const target = event.target as HTMLElement | null;
            const tag = target?.tagName;
            if (
                tag === "INPUT" ||
                tag === "TEXTAREA" ||
                tag === "SELECT" ||
                target?.isContentEditable
            ) {
                return;
            }
            event.preventDefault();
            filterInputRef.current?.focus();
        }

        document.addEventListener("keydown", handleKeyDown);
        return () => document.removeEventListener("keydown", handleKeyDown);
    }, []);

    useEffect(() => {
        const timer = setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS);
        return () => clearTimeout(timer);
    }, [query]);

    useEffect(() => {
        const trimmed = debouncedQuery.trim();
        if (trimmed.length < SERVER_SEARCH_MIN_QUERY) {
            setServerTargets(null);
            setServerSearchError(null);
            return;
        }

        let active = true;
        setServerSearchError(null);

        async function searchOnServer() {
            try {
                const { data, error } = await supabase.rpc("search_targets", {
                    q: trimmed,
                    lim: SERVER_SEARCH_LIMIT,
                });
                if (!active) return;
                if (error) {
                    setServerTargets(null);
                    setServerSearchError(error.message || "Server search failed");
                    return;
                }
                setServerTargets(
                    dedupeTargetsById(
                        Array.isArray(data) ? (data as SidebarCredential[]) : [],
                    ),
                );
            } catch (err) {
                if (!active) return;
                setServerTargets(null);
                setServerSearchError(
                    err instanceof Error ? err.message : "Server search failed",
                );
            }
        }

        searchOnServer();

        return () => {
            active = false;
        };
    }, [debouncedQuery]);

    const serverSearchActive = serverTargets !== null;
    const sourceCredentials = serverTargets ?? credentials;

    const filteredCredentials = useMemo(
        () =>
            filterTargets(sourceCredentials, {
                query: serverSearchActive ? "" : debouncedQuery,
                statuses: activeStatuses,
                chatTypes: activeChatTypes,
            }),
        [
            sourceCredentials,
            serverSearchActive,
            debouncedQuery,
            activeStatuses,
            activeChatTypes,
        ],
    );

    const filtersActive =
        debouncedQuery.trim().length > 0 ||
        activeStatuses.length > 0 ||
        activeChatTypes.length > 0;

    function toggleStatus(status: string) {
        setActiveStatuses((current) =>
            current.includes(status)
                ? current.filter((value) => value !== status)
                : [...current, status],
        );
    }

    function toggleChatType(chatType: string) {
        setActiveChatTypes((current) =>
            current.includes(chatType)
                ? current.filter((value) => value !== chatType)
                : [...current, chatType],
        );
    }

    function clearFilters() {
        setQuery("");
        setDebouncedQuery("");
        setActiveStatuses([]);
        setActiveChatTypes([]);
    }

    useEffect(() => {
        async function fetchCreds() {
            console.log("[Sidebar] Fetching credentials...");

            try {
                // Fetch credentials via authenticated view
                let creds: SidebarCredential[] | null = null;
                let error: { message: string } | null = null;

                const { data: dataNew, error: errNew } = await supabase
                    .from("discovered_credentials_public")
                    .select("id, created_at, source, status, meta, confidence_score, collection_yield_score, chat_member_count, chat_id, chat_name, chat_type")
                    .not("id", "is", null)
                    // Prefer new collection_yield_score column when the view exposes it;
                    // fall back to confidence_score for the legacy alias.
                    .order("collection_yield_score", { ascending: false, nullsFirst: false })
                    .order("created_at", { ascending: false })
                    .limit(TARGET_FETCH_LIMIT);

                if (errNew && /confidence_score|collection_yield_score|chat_member_count|chat_name|chat_type|status/.test(errNew.message)) {
                    // View is on old schema — migration 004 not applied yet.
                    console.warn("[Sidebar] migration 004 not applied; falling back");
                    const { data: dataOld, error: errOld } = await supabase
                        .from("discovered_credentials_public")
                        .select("id, created_at, source, meta")
                        .not("id", "is", null)
                        .order("created_at", { ascending: false })
                        .limit(TARGET_FETCH_LIMIT);
                    creds = dataOld;
                    error = errOld;
                } else {
                    creds = dataNew;
                    error = errNew;
                }

                if (error) {
                    console.error("[Sidebar] Error fetching credentials:", error.message);
                    return;
                }

                const sorted = creds || [];

                console.log(`[Sidebar] Found ${sorted.length} bots with messages (sources: ${[...new Set(sorted.map(c => c.source))].join(', ') || 'none'})`);
                setCredentials(sorted);
            } catch (err) {
                console.error("[Sidebar] Exception fetching creds:", err);
            } finally {
                setLoading(false);
            }
        }

        fetchCreds();

        // NOTE: Raw realtime subscription removed.
        // After RLS hardening, anon cannot SELECT from exfiltrated_messages.
        // Authenticated users should use safe polling or refetch-on-focus instead.
        // Real-time updates require authenticated Supabase Realtime with RLS policies.
    }, []);

    return (
        <div className="w-full h-full flex flex-col bg-slate-50 overflow-y-auto border-r">
            <div className="p-4 border-b bg-white sticky top-0 z-10">
                <h2 className="font-bold text-lg flex items-center gap-2 text-slate-800">
                    <LucideTarget className="text-red-600" /> The Prawn Hunter
                </h2>
                <div className="mt-3 grid grid-cols-2 gap-1 rounded-md bg-slate-100 p-1">
                    <button
                        type="button"
                        onClick={() => onViewChange("findings")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "findings"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <ListChecks className="h-3.5 w-3.5" />
                        <span className="truncate">Findings</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => onViewChange("search")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "search"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <Search className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">Search</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => onViewChange("watchlists")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "watchlists"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <BellRing className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">Watchlists</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => onViewChange("chat")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "chat"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <MessageSquareText className="h-3.5 w-3.5" />
                        <span className="truncate">Chat Feed</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => onViewChange("botTelemetry")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "botTelemetry"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <Radar className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">Bot Intel</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => onViewChange("globalTelemetry")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "globalTelemetry"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <BarChart3 className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">All Telemetry</span>
                    </button>
                    <button
                        type="button"
                        onClick={() => onViewChange("dashboard")}
                        className={`flex min-w-0 items-center justify-center gap-1 rounded px-1.5 py-1.5 text-xs font-semibold transition-colors ${
                            activeView === "dashboard"
                                ? "bg-white text-slate-900 shadow-sm"
                                : "text-slate-500 hover:text-slate-800"
                        }`}
                    >
                        <LayoutDashboard className="h-3.5 w-3.5 shrink-0" />
                        <span className="truncate">Dashboard</span>
                    </button>
                </div>
            </div>
            <div className="border-b bg-slate-100 px-4 py-2 font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-slate-500">
                Drill-down targets
            </div>
            <div className="border-b bg-white p-3 space-y-2">
                <div className="relative">
                    <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
                    <input
                        ref={filterInputRef}
                        type="search"
                        aria-label="Search targets"
                        placeholder="Search bot, chat title, chat name  (press /)"
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        className="w-full rounded border border-slate-200 bg-slate-50 py-1.5 pl-7 pr-2 text-xs text-slate-800 placeholder:text-slate-400 focus:border-slate-400 focus:outline-none focus:ring-1 focus:ring-slate-400"
                    />
                </div>
                <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by status">
                    {STATUS_FILTERS.map((status) => {
                        const pressed = activeStatuses.includes(status);
                        return (
                            <button
                                key={status}
                                type="button"
                                aria-pressed={pressed}
                                onClick={() => toggleStatus(status)}
                                className={`rounded px-1.5 py-0.5 font-mono text-[10px] uppercase transition-colors ${
                                    pressed
                                        ? "bg-slate-800 text-white"
                                        : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                                }`}
                            >
                                {status}
                            </button>
                        );
                    })}
                </div>
                <div className="flex flex-wrap gap-1" role="group" aria-label="Filter by chat type">
                    {CHAT_TYPE_FILTERS.map((chatType) => {
                        const pressed = activeChatTypes.includes(chatType);
                        return (
                            <button
                                key={chatType}
                                type="button"
                                aria-pressed={pressed}
                                onClick={() => toggleChatType(chatType)}
                                className={`rounded px-1.5 py-0.5 font-mono text-[10px] uppercase transition-colors ${
                                    pressed
                                        ? "bg-cyan-700 text-white"
                                        : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                                }`}
                            >
                                {chatType}
                            </button>
                        );
                    })}
                </div>
                <div
                    role="status"
                    aria-live="polite"
                    className="font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500"
                >
                    {loading
                        ? "Loading targets…"
                        : `${filteredCredentials.length} of ${sourceCredentials.length} targets`}
                </div>
                {serverSearchError && (
                    <p
                        role="note"
                        data-testid="server-search-note"
                        className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] text-amber-800"
                    >
                        Server search unavailable — showing matches from loaded targets.
                    </p>
                )}
            </div>
            <div
                role="list"
                aria-label="Drill-down targets"
                aria-busy={loading}
                className="flex flex-col"
            >
                {loading && (
                    <div
                        data-testid="targets-skeleton"
                        aria-busy="true"
                        aria-label="Loading targets"
                        className="border-b p-4"
                    >
                        <div className="space-y-3">
                            {Array.from({ length: 4 }, (_, index) => (
                                <div
                                    key={index}
                                    className="animate-pulse space-y-2 motion-reduce:animate-none"
                                >
                                    <div className="h-3 w-2/3 rounded bg-slate-200" />
                                    <div className="h-2.5 w-1/3 rounded bg-slate-100" />
                                </div>
                            ))}
                        </div>
                    </div>
                )}
                {!loading && filteredCredentials.length === 0 && (
                    <div className="border-b p-4 text-sm text-slate-500">
                        <p className="font-semibold text-slate-700">
                            {credentials.length === 0 && !filtersActive
                                ? "No targets found"
                                : "No targets match"}
                        </p>
                        <p className="mt-1 text-xs text-slate-500">
                            {credentials.length === 0 && !filtersActive
                                ? "Run a scan to discover exposed bots; new targets appear here automatically."
                                : "Try a broader search or clear the active filters."}
                        </p>
                        {credentials.length > 0 && filtersActive && (
                            <button
                                type="button"
                                onClick={clearFilters}
                                className="mt-2 rounded border border-slate-300 bg-white px-2 py-1 text-xs font-bold text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
                            >
                                Clear filters
                            </button>
                        )}
                    </div>
                )}
                {filteredCredentials.map((cred) => (
                    <div role="listitem" key={cred.id} className="contents">
                    <button
                        onClick={() => onSelect(cred)}
                        className={`p-4 border-b text-left hover:bg-slate-100 transition-colors ${
                            selected?.id === cred.id ? "bg-blue-50 border-l-4 border-l-blue-500" : ""
                        }`}
                    >
                        <div className="flex justify-between w-full mb-1">
                            <span className="font-semibold text-slate-800 truncate">
                                {cred.meta?.bot_username
                                    ? `@${cred.meta.bot_username} / ${cred.meta.bot_id || '?'}`
                                    : (cred.meta?.bot_id ? `@unknown / ${cred.meta.bot_id}` : (cred.meta?.chat_title || "Unknown Chat"))}
                            </span>
                            <span className="text-xs text-slate-400">
                                {new Date(cred.created_at).toLocaleDateString()}
                            </span>
                        </div>
                        <div className="text-sm text-slate-500 truncate flex items-center gap-1">
                            <span className="bg-slate-200 px-1 py-0.5 rounded text-[10px] uppercase font-mono">{cred.source}</span>
                            <ConfidenceBadge score={cred.collection_yield_score ?? cred.confidence_score ?? null} />
                            {typeof cred.chat_member_count === "number" && cred.chat_member_count > 1 && (
                                <span className="bg-blue-50 text-blue-700 px-1 py-0.5 rounded text-[10px] font-mono" title={`${cred.chat_member_count} members`}>
                                    👥{cred.chat_member_count >= 1000 ? `${Math.round(cred.chat_member_count / 1000)}k` : cred.chat_member_count}
                                </span>
                            )}
                            <span className="font-mono text-xs opacity-70 truncate">ID: {cred.meta?.bot_id || cred.id.slice(0, 8)}</span>
                        </div>
                        {(cred.chat_name || cred.meta?.chat_title) && (
                            <div className="text-xs text-slate-500 truncate mt-1">
                                <span className="bg-cyan-50 text-cyan-700 px-1 py-0.5 rounded font-mono" title={cred.chat_name || cred.meta?.chat_title}>
                                    {chatTypeLabel(cred.chat_type)}: {cred.chat_name || cred.meta?.chat_title}
                                </span>
                            </div>
                        )}
                    </button>
                    </div>
                ))}
            </div>
        </div>
    );
}
