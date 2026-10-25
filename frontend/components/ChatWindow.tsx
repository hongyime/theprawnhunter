"use client";

import { useCallback, useEffect, useLayoutEffect, useState, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { useAuth } from "@/lib/auth";
import {
    ChevronDown,
    ChevronUp,
    FileDown,
    Globe2,
    LucideSend,
    RefreshCw,
    Search as SearchIcon,
    Server,
    TerminalSquare,
    TriangleAlert,
    X,
} from "lucide-react";
import type { Credential, InfrastructureContext } from "@/app/page";
import { HighlightedSnippet, type EvidenceSearchRow } from "./SearchView";

const SEARCH_DEBOUNCE_MS = 300;
const SEARCH_RESULT_LIMIT = 50;
const SEARCH_MIN_QUERY = 2;
const PAGE_SIZE = 50;
const POLL_INTERVAL_MS = 3000;
const POLL_ENABLED = process.env.NEXT_PUBLIC_CHAT_POLL_ENABLED !== "false";
const SCROLL_TOP_THRESHOLD = 40;
const AUTO_SCROLL_BOTTOM_THRESHOLD = 120;

const useIsomorphicLayoutEffect =
    typeof window !== "undefined" ? useLayoutEffect : useEffect;

export default function ChatWindow({ credential }: { credential: Credential | null }) {
    const { session } = useAuth();
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [isProfileExpanded, setIsProfileExpanded] = useState(true);
    const [loading, setLoading] = useState(false);
    const [authError, setAuthError] = useState<string | null>(null);
    const [searchQuery, setSearchQuery] = useState("");
    const [searchResults, setSearchResults] = useState<EvidenceSearchRow[]>([]);
    const [searchLoading, setSearchLoading] = useState(false);
    const [searchError, setSearchError] = useState<string | null>(null);
    const [searchHasSearched, setSearchHasSearched] = useState(false);
    const [retryToken, setRetryToken] = useState(0);
    const [loadingOlder, setLoadingOlder] = useState(false);
    const [olderError, setOlderError] = useState<string | null>(null);
    const [hasMore, setHasMore] = useState(true);
    const bottomRef = useRef<HTMLDivElement>(null);
    const listRef = useRef<HTMLDivElement>(null);
    const searchInputRef = useRef<HTMLInputElement>(null);
    const nextOffsetRef = useRef(0);
    const hasMoreRef = useRef(true);
    const loadingOlderRef = useRef(false);
    const loadingInitialRef = useRef(false);
    const pendingPrependScrollRef = useRef<number | null>(null);
    const lastNewestIdRef = useRef<string | null>(null);
    const credentialId = credential?.id;

    const trimmedQuery = searchQuery.trim();
    const searchActive = trimmedQuery.length >= SEARCH_MIN_QUERY;

    const clearSearch = useCallback(() => {
        setSearchQuery("");
        setSearchResults([]);
        setSearchLoading(false);
        setSearchError(null);
        setSearchHasSearched(false);
    }, []);

    useEffect(() => {
        clearSearch();
        setRetryToken(0);
    }, [credentialId, clearSearch]);

    useEffect(() => {
        if (!credentialId) return;

        let cancelled = false;
        loadingInitialRef.current = true;
        nextOffsetRef.current = 0;
        hasMoreRef.current = true;
        setHasMore(true);
        setOlderError(null);
        loadingOlderRef.current = false;
        setLoadingOlder(false);

        async function fetchNewestPage(isPolling: boolean) {
            if (!isPolling) {
                setLoading(true);
                setAuthError(null);
                setMessages([]);
                lastNewestIdRef.current = null;
            }

            // Gate: must be authenticated to read evidence
            if (!session) {
                setAuthError("Sign in required to view exfiltrated messages.");
                if (!isPolling) {
                    setLoading(false);
                    loadingInitialRef.current = false;
                }
                return;
            }

            // Authenticated query to evidence_redacted view (truncated + masked content).
            // Newest-first page; the render order is restored by mergeMessages().
            const { data, error } = await supabase
                .from("evidence_redacted")
                .select("*")
                .eq("credential_id", credentialId)
                .order("created_at", { ascending: false })
                .range(0, PAGE_SIZE - 1);

            if (cancelled) return;

            if (error) {
                // A failed poll is silent; a failed first page is explicit.
                if (isPolling) return;
                setAuthError(describeLoadError(error as QueryFailure));
                setLoading(false);
                loadingInitialRef.current = false;
                return;
            }

            const rows = (data as ChatMessage[] | null) ?? [];

            if (isPolling) {
                // Refresh only the newest page and merge by id so paginated
                // history is never clobbered.
                setMessages((previous) => mergeMessages(previous, rows));
                return;
            }

            setMessages(mergeMessages([], rows));
            nextOffsetRef.current = rows.length;
            const more = rows.length === PAGE_SIZE;
            hasMoreRef.current = more;
            setHasMore(more);
            setLoading(false);
            loadingInitialRef.current = false;
        }

        void fetchNewestPage(false);

        // Active polling is behind NEXT_PUBLIC_CHAT_POLL_ENABLED until the
        // realtime/replacement path lands.
        let intervalId: ReturnType<typeof setInterval> | undefined;
        let handleVisibilityChange: (() => void) | undefined;

        if (POLL_ENABLED) {
            intervalId = setInterval(() => {
                if (typeof document !== "undefined" && document.visibilityState === "hidden") {
                    return;
                }
                void fetchNewestPage(true);
            }, POLL_INTERVAL_MS);

            handleVisibilityChange = () => {
                if (typeof document !== "undefined" && document.visibilityState === "visible") {
                    void fetchNewestPage(true);
                }
            };

            if (typeof document !== "undefined") {
                document.addEventListener("visibilitychange", handleVisibilityChange);
            }
        }

        return () => {
            cancelled = true;
            if (intervalId) clearInterval(intervalId);
            if (handleVisibilityChange && typeof document !== "undefined") {
                document.removeEventListener("visibilitychange", handleVisibilityChange);
            }
        };
    }, [credentialId, session]);

    const loadOlderPage = useCallback(async () => {
        if (!credentialId || !session) return;
        if (loadingOlderRef.current || loadingInitialRef.current || !hasMoreRef.current) return;

        loadingOlderRef.current = true;
        setLoadingOlder(true);
        setOlderError(null);

        const offset = nextOffsetRef.current;
        const { data, error } = await supabase
            .from("evidence_redacted")
            .select("*")
            .eq("credential_id", credentialId)
            .order("created_at", { ascending: false })
            .range(offset, offset + PAGE_SIZE - 1);

        if (error) {
            setOlderError(`Failed to load older messages: ${error.message}`);
            setLoadingOlder(false);
            loadingOlderRef.current = false;
            return;
        }

        const rows = (data as ChatMessage[] | null) ?? [];
        nextOffsetRef.current = offset + rows.length;
        const more = rows.length === PAGE_SIZE;
        hasMoreRef.current = more;
        setHasMore(more);
        setLoadingOlder(false);
        loadingOlderRef.current = false;

        if (rows.length > 0) {
            const container = listRef.current;
            pendingPrependScrollRef.current = container ? container.scrollHeight : null;
            setMessages((previous) => mergeMessages(previous, rows));
        }
    }, [credentialId, session]);

    const handleScroll = useCallback(() => {
        if (searchActive) return;
        const container = listRef.current;
        if (!container || container.scrollTop > SCROLL_TOP_THRESHOLD) return;
        void loadOlderPage();
    }, [searchActive, loadOlderPage]);

    useEffect(() => {
        if (!searchActive || !credentialId) {
            setSearchResults([]);
            setSearchLoading(false);
            setSearchError(null);
            setSearchHasSearched(false);
            return;
        }

        let active = true;
        setSearchLoading(true);
        setSearchError(null);

        const timer = window.setTimeout(async () => {
            const { data, error } = await supabase.rpc("search_evidence", {
                q: trimmedQuery,
                credential_id: credentialId,
                lim: SEARCH_RESULT_LIMIT,
            });
            if (!active) return;
            if (error) {
                setSearchResults([]);
                setSearchError(`Search failed: ${error.message}`);
            } else {
                setSearchResults((data as EvidenceSearchRow[] | null) ?? []);
                setSearchError(null);
            }
            setSearchHasSearched(true);
            setSearchLoading(false);
        }, SEARCH_DEBOUNCE_MS);

        return () => {
            active = false;
            window.clearTimeout(timer);
        };
    }, [searchActive, trimmedQuery, credentialId, retryToken]);

    useEffect(() => {
        if (typeof document === "undefined") return;

        function handleKeyDown(event: KeyboardEvent) {
            if (event.key === "Escape" && searchActive) {
                clearSearch();
                return;
            }
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
            searchInputRef.current?.focus();
        }

        document.addEventListener("keydown", handleKeyDown);
        return () => document.removeEventListener("keydown", handleKeyDown);
    }, [searchActive, clearSearch]);

    useEffect(() => {
        if (searchActive) return;
        const newestId = messages.length > 0 ? messages[messages.length - 1].id : null;
        if (!newestId || newestId === lastNewestIdRef.current) return;
        lastNewestIdRef.current = newestId;

        const container = listRef.current;
        const nearBottom =
            !container ||
            container.scrollHeight - container.scrollTop - container.clientHeight <
                AUTO_SCROLL_BOTTOM_THRESHOLD;
        if (nearBottom) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [messages, searchActive]);

    useIsomorphicLayoutEffect(() => {
        const previousHeight = pendingPrependScrollRef.current;
        if (previousHeight === null) return;
        pendingPrependScrollRef.current = null;

        const container = listRef.current;
        if (!container) return;
        const delta = container.scrollHeight - previousHeight;
        if (delta > 0) container.scrollTop += delta;
    }, [messages]);

    if (!credential) {
        return (
            <div className="flex-1 flex items-center justify-center bg-slate-200 text-slate-600">
                Select a chat to view exfiltrated messages
            </div>
        );
    }

    if (authError) {
        return (
            <div className="flex-1 flex items-center justify-center bg-slate-200 text-slate-600">
                <div className="text-center">
                    <p className="text-red-600 mb-2">{authError}</p>
                    <a href="/signin" className="text-cyan-600 hover:underline">
                        Sign in
                    </a>
                </div>
            </div>
        );
    }

    if (loading) {
        return (
            <div className="flex-1 flex items-center justify-center bg-slate-200 text-slate-600">
                Loading messages...
            </div>
        );
    }

    const displayName = credential.meta?.bot_username
        ? `@${credential.meta.bot_username}`
        : credential.chat_name || credential.meta?.chat_title || "Unknown Bot";
    const chatName = credential.chat_name || credential.meta?.chat_title;
    const gatewayTelemetry = credential.meta?.gateway_telemetry;
    const infrastructureContext = credential.meta?.infrastructure_context;
    const commands = (gatewayTelemetry?.command_dictionary || [])
        .map((item) => item.command)
        .filter((command): command is string => Boolean(command));
    const infrastructureEndpoints = collectInfrastructureEndpoints(infrastructureContext);
    const hasEndpointProfile = Boolean(
        gatewayTelemetry?.configured_webhook_url ||
        gatewayTelemetry?.resolved_ip_address ||
        commands.length > 0 ||
        infrastructureEndpoints.length > 0
    );

    const searchSummary = searchLoading
        ? "Searching this chat…"
        : searchError
            ? "Search error"
            : searchActive && searchHasSearched
                ? `${searchResults.length} ${searchResults.length === 1 ? "match" : "matches"} in this chat`
                : searchActive
                    ? "Searching this chat…"
                    : trimmedQuery
                        ? "Type one more character to search."
                        : "Press / to search this chat.";

    return (
        <div className="flex-1 flex flex-col h-full bg-[#E5DDD5]">
            <div className="border-b bg-white/90 shadow-sm backdrop-blur">
                <div className="p-3 flex items-center gap-3">
                    <div className="flex flex-col min-w-0">
                        <span className="font-semibold text-slate-800 truncate">{displayName}</span>
                        <div className="flex items-center gap-2 mt-0.5">
                            <span className="bg-slate-200 px-1.5 py-0.5 rounded text-[10px] uppercase font-mono text-slate-600">
                                {credential.source}
                            </span>
                            <span className="text-xs font-mono text-slate-400">
                                ID: {credential.meta?.bot_id || credential.id.slice(0, 8)}
                            </span>
                            {chatName && (
                                <span className="text-xs text-slate-500 truncate" title={chatName}>
                                    {chatTypeLabel(credential.chat_type)}: {chatName}
                                </span>
                            )}
                        </div>
                    </div>
                </div>
                <form
                    role="search"
                    className="mx-3 mb-3"
                    onSubmit={(event) => event.preventDefault()}
                >
                    <div className="flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 shadow-sm focus-within:border-[#197e74] focus-within:ring-2 focus-within:ring-[#197e74]/25">
                        <SearchIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-slate-400" />
                        <input
                            ref={searchInputRef}
                            type="search"
                            value={searchQuery}
                            onChange={(event) => setSearchQuery(event.target.value)}
                            aria-label="Search this chat"
                            aria-describedby="chat-search-summary"
                            placeholder="Search this chat…  (press / to focus)"
                            className="w-full bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
                        />
                        <kbd className="hidden shrink-0 rounded border border-slate-300 bg-slate-50 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 md:inline">
                            /
                        </kbd>
                        {searchQuery && (
                            <button
                                type="button"
                                onClick={clearSearch}
                                aria-label="Clear search"
                                className="flex shrink-0 items-center gap-1 rounded border border-slate-300 bg-slate-50 px-2 py-1 text-[11px] font-bold text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
                            >
                                <X className="h-3 w-3" aria-hidden="true" /> Clear
                            </button>
                        )}
                    </div>
                    <p
                        id="chat-search-summary"
                        role="status"
                        aria-live="polite"
                        className="mt-2 text-xs text-slate-600"
                    >
                        {searchSummary}
                    </p>
                </form>
                {hasEndpointProfile && (
                    <div className="mx-3 mb-3 rounded border border-white/60 bg-white/70 shadow-sm backdrop-blur-md">
                        <button
                            type="button"
                            onClick={() => setIsProfileExpanded((value) => !value)}
                            className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left"
                        >
                            <span className="flex items-center gap-2 text-sm font-semibold text-slate-800">
                                <Server className="h-4 w-4 text-cyan-600" />
                                Endpoint Profile
                            </span>
                            {isProfileExpanded ? (
                                <ChevronUp className="h-4 w-4 text-slate-500" />
                            ) : (
                                <ChevronDown className="h-4 w-4 text-slate-500" />
                            )}
                        </button>
                        {isProfileExpanded && (
                            <div className="px-3 pb-3 space-y-1 text-xs">
                                {gatewayTelemetry?.configured_webhook_url && (
                                    <div className="flex items-center gap-2">
                                        <Globe2 className="h-3 w-3 text-slate-400" />
                                        <span className="text-slate-600">Webhook:</span>
                                        <code className="font-mono text-cyan-600 truncate">
                                            {gatewayTelemetry.configured_webhook_url}
                                        </code>
                                    </div>
                                )}
                                {gatewayTelemetry?.resolved_ip_address && (
                                    <div className="flex items-center gap-2">
                                        <Server className="h-3 w-3 text-slate-400" />
                                        <span className="text-slate-600">IP:</span>
                                        <code className="font-mono text-cyan-600">
                                            {gatewayTelemetry.resolved_ip_address}
                                        </code>
                                    </div>
                                )}
                                {commands.length > 0 && (
                                    <div className="flex items-start gap-2">
                                        <TerminalSquare className="h-3 w-3 text-slate-400 mt-0.5" />
                                        <span className="text-slate-600">Commands:</span>
                                        <div className="flex flex-wrap gap-1">
                                            {commands.slice(0, 20).map((cmd, i) => (
                                                <code
                                                    key={i}
                                                    className="px-1 rounded bg-slate-100 text-slate-700 font-mono"
                                                >
                                                    {cmd}
                                                </code>
                                            ))}
                                        </div>
                                    </div>
                                )}
                                {infrastructureEndpoints.length > 0 && (
                                    <div className="flex items-start gap-2">
                                        <LucideSend className="h-3 w-3 text-slate-400 mt-0.5" />
                                        <span className="text-slate-600">Endpoints:</span>
                                        <div className="flex flex-wrap gap-1">
                                            {infrastructureEndpoints.map((endpoint, i) => (
                                                <a
                                                    key={i}
                                                    href={endpoint}
                                                    target="_blank"
                                                    rel="noopener noreferrer"
                                                    className="px-1 rounded bg-slate-100 text-cyan-600 hover:underline font-mono text-[10px]"
                                                >
                                                    {endpoint}
                                                </a>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                )}
            </div>
            <div
                ref={listRef}
                role="log"
                aria-label="Message history"
                onScroll={handleScroll}
                className="flex-1 overflow-y-auto p-4 space-y-2"
                aria-busy={searchActive && searchLoading}
            >
                {searchActive ? (
                    <ChatSearchResults
                        loading={searchLoading}
                        error={searchError}
                        hasSearched={searchHasSearched}
                        results={searchResults}
                        query={trimmedQuery}
                        onRetry={() => setRetryToken((value) => value + 1)}
                        onClear={clearSearch}
                    />
                ) : (
                    <>
                        {olderError ? (
                            <div
                                role="alert"
                                className="flex flex-wrap items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
                            >
                                <TriangleAlert aria-hidden="true" className="h-3.5 w-3.5 shrink-0" />
                                <span className="min-w-0 flex-1">{olderError}</span>
                                <button
                                    type="button"
                                    onClick={() => void loadOlderPage()}
                                    className="flex items-center gap-1 rounded border border-red-300 bg-white px-2 py-1 font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
                                >
                                    <RefreshCw className="h-3 w-3" aria-hidden="true" /> Retry
                                </button>
                            </div>
                        ) : loadingOlder ? (
                            <div
                                role="status"
                                className="flex items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600"
                            >
                                <span
                                    aria-hidden="true"
                                    className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-slate-300 border-t-[#197e74] motion-reduce:animate-none"
                                />
                                Loading earlier messages…
                            </div>
                        ) : !hasMore && messages.length > 0 ? (
                            <p className="py-1 text-center font-mono text-[11px] uppercase tracking-wide text-slate-500">
                                Beginning of history
                            </p>
                        ) : null}
                        {messages.filter(m => m.content).map((message, i) => (
                            <div key={message.id || i} className="bg-white rounded-lg p-2 shadow-sm">
                                <div className="flex items-center gap-2 mb-1">
                                    <span className="text-xs font-semibold text-slate-800">
                                        {message.sender_pseudonym || "Unknown"}
                                    </span>
                                    <span className="text-xs text-slate-400">
                                        {new Date(message.created_at).toLocaleString()}
                                    </span>
                                </div>
                                <p className="text-sm text-slate-700 whitespace-pre-wrap">
                                    {message.content}
                                </p>
                                {message.media_type && message.media_type !== "text" && (
                                    <div className="mt-1 text-xs text-cyan-600 flex items-center gap-1">
                                        <FileDown className="h-3 w-3" />
                                        {message.media_type}
                                    </div>
                                )}
                            </div>
                        ))}
                        <div ref={bottomRef} />
                    </>
                )}
            </div>
        </div>
    );
}

function ChatSearchResults({
    loading,
    error,
    hasSearched,
    results,
    query,
    onRetry,
    onClear,
}: {
    loading: boolean;
    error: string | null;
    hasSearched: boolean;
    results: EvidenceSearchRow[];
    query: string;
    onRetry: () => void;
    onClear: () => void;
}) {
    if (loading) {
        return (
            <div
                role="status"
                className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600"
            >
                <span
                    aria-hidden="true"
                    className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-[#197e74] motion-reduce:animate-none"
                />
                Searching this chat…
            </div>
        );
    }

    if (error) {
        return (
            <div
                role="alert"
                className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"
            >
                <div className="flex flex-wrap items-center gap-3">
                    <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
                    <span className="min-w-0 flex-1">{error}</span>
                    <button
                        type="button"
                        onClick={onRetry}
                        className="flex items-center gap-1.5 rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
                    >
                        <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" /> Retry
                    </button>
                </div>
                <p className="mt-2 text-xs text-red-700">
                    Clear the search to return to the conversation.
                </p>
            </div>
        );
    }

    if (!hasSearched) {
        return null;
    }

    if (results.length === 0) {
        return (
            <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
                <h2 className="font-bold text-slate-900">No matches in this chat</h2>
                <p className="mt-1 text-sm text-slate-600">
                    No captured messages in this conversation match “{query}”.
                </p>
                <button
                    type="button"
                    onClick={onClear}
                    className="mt-3 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
                >
                    Back to the conversation
                </button>
            </div>
        );
    }

    return (
        <>
            {results.map((row) => {
                const senderKey = row.sender_key || "unknown";
                return (
                    <article
                        key={row.id}
                        className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm"
                    >
                        <div className="flex flex-wrap items-center gap-2">
                            <span
                                title={senderKey}
                                className="rounded border border-[#197e74]/30 bg-[#197e74]/10 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-[#12645c]"
                            >
                                {senderKey}
                            </span>
                            {row.sender_pseudonym && row.sender_pseudonym !== senderKey && (
                                <span className="font-mono text-[10px] text-slate-400">
                                    {row.sender_pseudonym}
                                </span>
                            )}
                            <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-slate-600">
                                {row.media_type || "text"}
                            </span>
                            <time
                                dateTime={row.created_at || undefined}
                                className="ml-auto whitespace-nowrap font-mono text-[11px] text-slate-500"
                            >
                                {row.created_at ? new Date(row.created_at).toLocaleString() : "Unknown time"}
                            </time>
                        </div>
                        <div className="mt-2">
                            <HighlightedSnippet snippet={row.snippet} />
                        </div>
                    </article>
                );
            })}
        </>
    );
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

function collectInfrastructureEndpoints(context: InfrastructureContext | undefined): string[] {    if (!context) return [];
    const endpoints: string[] = [];
    if (context.api_urls && Array.isArray(context.api_urls)) {
        endpoints.push(...context.api_urls.filter((u): u is string => typeof u === 'string' && u.length > 0));
    }
    if (context.webhook_urls && Array.isArray(context.webhook_urls)) {
        endpoints.push(...context.webhook_urls.filter((u): u is string => typeof u === 'string' && u.length > 0));
    }
    return [...new Set(endpoints)];
}

interface QueryFailure {
    message: string;
    code?: string;
    status?: number;
}

function describeLoadError(error: QueryFailure): string {
    const isAuth =
        error.status === 401 ||
        error.status === 403 ||
        error.code === "PGRST301" ||
        error.code === "42501" ||
        /permission|denied|jwt|auth|rls/i.test(error.message);
    return isAuth
        ? "Sign in required to view exfiltrated messages."
        : `Failed to load messages: ${error.message}`;
}

function mergeMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
    if (incoming.length === 0) return current;
    const byId = new Map<string, ChatMessage>();
    for (const message of current) byId.set(message.id, message);
    for (const message of incoming) byId.set(message.id, message);
    return Array.from(byId.values()).sort((left, right) => {
        const delta = new Date(left.created_at).getTime() - new Date(right.created_at).getTime();
        return delta !== 0 ? delta : left.id.localeCompare(right.id);
    });
}

interface ChatMessage {
    id: string;
    credential_id: string;
    sender_pseudonym: string | null;
    content: string | null;
    media_type: string | null;
    is_broadcasted: boolean;
    created_at: string;
}
