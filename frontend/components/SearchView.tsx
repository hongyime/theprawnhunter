"use client";

import { useEffect, useRef, useState } from "react";
import {
  MessageSquareText,
  RefreshCw,
  Search as SearchIcon,
  Sparkles,
  TriangleAlert,
} from "lucide-react";

import { supabase } from "@/lib/supabase";

export type EvidenceSearchRow = {
  id: string;
  credential_id: string | null;
  sender_key: string | null;
  sender_pseudonym: string | null;
  snippet: string | null;
  media_type: string | null;
  created_at: string | null;
};

export const SEARCH_DEBOUNCE_MS = 300;
export const SEARCH_RESULT_LIMIT = 50;
export const SEARCH_MIN_QUERY = 3;
export const SEARCH_MIN_QUERY_HINT = "Type at least 3 characters";
export const SEMANTIC_NOTICE = "Semantic unavailable — showing keyword results";

type SearchMode = "fts" | "semantic";

type SemanticHit = {
  message_id: string;
  credential_id: string | null;
  distance: number | null;
};

type HydrationFileMeta = {
  transcript_text?: unknown;
  ocr_text?: unknown;
};

type HydrationRow = {
  id: string;
  credential_id?: string | null;
  sender_key?: string | null;
  sender_pseudonym?: string | null;
  media_type?: string | null;
  created_at?: string | null;
  content?: string | null;
  file_meta?: HydrationFileMeta | null;
};

/** Same-origin proxy; it injects `MONITOR_API_KEY` server-side. */
export const SEMANTIC_PROXY_PATH = "/api/semantic-search";

export const SEMANTIC_HYDRATION_SELECT =
  "id, credential_id, sender_key, sender_pseudonym, media_type, created_at, content, file_meta";

export const SNIPPET_MAX_LENGTH = 200;

/**
 * Build a plain-text snippet centred on the query term.
 *
 * The returned string is *text*, not HTML: the only markup ever added is a
 * `<b>…</b>` pair around the matched substring, which the existing
 * `HighlightedSnippet` renderer interprets safely. Everything else (including
 * hostile input such as `<script>`) becomes inert React text, so no raw HTML
 * is injected.
 */
export function buildSnippet(
  content: string | null | undefined,
  query: string,
): string | null {
  if (!content) return null;
  const text = content.replace(/\s+/g, " ").trim();
  if (!text) return null;

  const needle = query.trim().toLowerCase();
  const matchIndex = needle ? text.toLowerCase().indexOf(needle) : -1;

  let start = 0;
  if (matchIndex >= 0) {
    const pad = Math.max(
      0,
      Math.floor((SNIPPET_MAX_LENGTH - needle.length) / 2),
    );
    start = Math.max(0, matchIndex - pad);
  }

  const prefix = start > 0 ? "…" : "";
  let end = Math.min(text.length, start + (SNIPPET_MAX_LENGTH - prefix.length));
  const hasSuffix = end < text.length;
  if (hasSuffix) {
    end -= prefix.length;
  }
  end = Math.max(start, end);

  const body = text.slice(start, end);
  const suffix = hasSuffix ? "…" : "";
  const relative = matchIndex - start;
  if (
    relative >= 0 &&
    needle.length > 0 &&
    relative + needle.length <= body.length
  ) {
    const before = body.slice(0, relative);
    const match = body.slice(relative, relative + needle.length);
    const after = body.slice(relative + needle.length);
    return `${prefix}${before}<b>${match}</b>${after}${suffix}`;
  }
  return `${prefix}${body}${suffix}`;
}

/**
 * Derive the searchable text for a hydrated semantic row.
 *
 * Matches the FTS document: `content` plus the operator-visible
 * `file_meta.transcript_text` and `file_meta.ocr_text`. Anything else is
 * ignored, and a missing or non-object `file_meta` degrades to the old
 * content-only behaviour. The returned value is plain text; only
 * `buildSnippet` ever adds a `<b>…</b>` pair.
 */
function derivedHydrationText(row: HydrationRow): string | null {
  const parts: string[] = [];
  if (typeof row.content === "string" && row.content.trim()) {
    parts.push(row.content.trim());
  }

  const meta = row.file_meta;
  if (meta && typeof meta === "object" && !Array.isArray(meta)) {
    for (const key of ["transcript_text", "ocr_text"] as const) {
      const value = meta[key];
      if (typeof value === "string" && value.trim()) {
        parts.push(value.trim());
      }
    }
  }

  return parts.length > 0 ? parts.join("\n") : null;
}

/**
 * Fetch the renderable fields for semantic hits from `evidence_redacted`.
 *
 * `semantic_search` only returns `(message_id, credential_id, distance)`, so
 * each hit is hydrated by id. Rows the authenticated view does not expose are
 * dropped; if no hit can be hydrated the caller falls back to FTS.
 */
async function hydrateSemanticRows(
  hits: SemanticHit[],
  query: string,
): Promise<EvidenceSearchRow[]> {
  const ids = Array.from(
    new Set(
      hits
        .map((hit) => hit.message_id)
        .filter((id): id is string => typeof id === "string" && id.length > 0),
    ),
  );
  if (ids.length === 0) return [];

  const { data, error } = await supabase
    .from("evidence_redacted")
    .select(SEMANTIC_HYDRATION_SELECT)
    .in("id", ids);
  if (error) throw new Error(error.message);

  const byId = new Map<string, HydrationRow>();
  for (const row of (data as HydrationRow[] | null) ?? []) {
    if (row && typeof row.id === "string") byId.set(row.id, row);
  }

  const hydrated: EvidenceSearchRow[] = [];
  for (const hit of hits) {
    const row = byId.get(hit.message_id);
    if (!row) continue;
    hydrated.push({
      id: row.id,
      credential_id: hit.credential_id ?? row.credential_id ?? null,
      sender_key: row.sender_key ?? null,
      sender_pseudonym: row.sender_pseudonym ?? null,
      snippet: buildSnippet(derivedHydrationText(row), query),
      media_type: row.media_type ?? null,
      created_at: row.created_at ?? null,
    });
  }

  if (hydrated.length === 0) {
    throw new Error("semantic hydration returned no rows");
  }
  return hydrated;
}

const CHIP_TONES = [
  "border-[#197e74]/30 bg-[#197e74]/10 text-[#12645c]",
  "border-[#2f5f9e]/30 bg-[#2f5f9e]/10 text-[#244b7d]",
  "border-[#9a5b1e]/30 bg-[#9a5b1e]/10 text-[#7c4817]",
  "border-[#7a3e8f]/30 bg-[#7a3e8f]/10 text-[#623272]",
  "border-[#a3403a]/30 bg-[#a3403a]/10 text-[#83312c]",
];

function chipTone(seed: string) {
  let hash = 0;
  for (let index = 0; index < seed.length; index += 1) {
    hash = (hash * 31 + seed.charCodeAt(index)) >>> 0;
  }
  return CHIP_TONES[hash % CHIP_TONES.length];
}

function senderInitials(senderKey: string | null) {
  const raw = (senderKey || "?").replace(/^user_/i, "").replace(/[^a-z0-9]/gi, "");
  return (raw.slice(0, 2) || "??").toUpperCase();
}

function formatTime(value: string | null) {
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

/**
 * Render a ts_headline snippet without injecting HTML.
 *
 * `search_evidence` returns snippets containing `<b>…</b>` emphasis tags. Only
 * that exact pair is interpreted: every segment is split out and any remaining
 * markup (including `<script>`, `<img>`, unclosed tags) is rendered as a React
 * text node, which React escapes. No `dangerouslySetInnerHTML` anywhere.
 */
export function HighlightedSnippet({ snippet }: { snippet: string | null }) {
  if (!snippet) {
    return <p className="text-sm italic text-slate-400">No preview available.</p>;
  }
  const segments = snippet.split(/(<b>[\s\S]*?<\/b>)/gi);
  return (
    <p className="text-sm leading-6 text-slate-700">
      {segments.map((segment, index) => {
        const highlighted = /^<b>([\s\S]*?)<\/b>$/i.exec(segment);
        if (!highlighted) {
          return <span key={index}>{segment}</span>;
        }
        return (
          <mark
            key={index}
            className="rounded-sm bg-amber-200 px-0.5 text-slate-900"
          >
            {highlighted[1]}
          </mark>
        );
      })}
    </p>
  );
}

export default function SearchView({
  onOpenChat,
}: {
  onOpenChat: (credentialId: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<EvidenceSearchRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hasSearched, setHasSearched] = useState(false);
  const [retryToken, setRetryToken] = useState(0);
  const [semantic, setSemantic] = useState(false);
  const [resultMode, setResultMode] = useState<SearchMode>("fts");
  const [notice, setNotice] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const trimmedQuery = query.trim();
  const queryTooShort =
    trimmedQuery.length > 0 && trimmedQuery.length < SEARCH_MIN_QUERY;

  // Keyboard shortcut: `/` focuses the search box while this view is mounted.
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
      inputRef.current?.focus();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    if (trimmedQuery.length < SEARCH_MIN_QUERY) {
      setResults([]);
      setLoading(false);
      setError(null);
      setHasSearched(false);
      setNotice(null);
      return;
    }

    let active = true;
    setLoading(true);
    setError(null);
    setNotice(null);

    async function runFts(): Promise<EvidenceSearchRow[]> {
      const { data, error: rpcError } = await supabase.rpc("search_evidence", {
        q: trimmedQuery,
        credential_id: null,
        lim: SEARCH_RESULT_LIMIT,
      });
      if (rpcError) throw new Error(rpcError.message);
      return (data as EvidenceSearchRow[] | null) ?? [];
    }

    async function runSemantic(): Promise<EvidenceSearchRow[]> {
      const response = await fetch(SEMANTIC_PROXY_PATH, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          q: trimmedQuery,
          credential_id: null,
          limit: SEARCH_RESULT_LIMIT,
        }),
      });
      if (!response.ok) {
        throw new Error(`semantic proxy returned ${response.status}`);
      }
      const payload = (await response.json()) as {
        results?: SemanticHit[];
        fallback?: boolean;
      };
      if (payload?.fallback) throw new Error("semantic fallback");
      return hydrateSemanticRows(payload?.results ?? [], trimmedQuery);
    }

    const timer = window.setTimeout(async () => {
      let rows: EvidenceSearchRow[] = [];
      let mode: SearchMode = "fts";
      let fallbackNotice: string | null = null;

      if (semantic) {
        try {
          rows = await runSemantic();
          mode = "semantic";
        } catch {
          fallbackNotice = SEMANTIC_NOTICE;
        }
      }

      if (mode === "fts") {
        try {
          rows = await runFts();
        } catch (error) {
          if (!active) return;
          setResults([]);
          setResultMode("fts");
          setNotice(fallbackNotice);
          setError(`Search failed: ${(error as Error).message}`);
          setHasSearched(true);
          setLoading(false);
          return;
        }
      }

      if (!active) return;
      setResults(rows);
      setResultMode(mode);
      setNotice(fallbackNotice);
      setError(null);
      setHasSearched(true);
      setLoading(false);
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [trimmedQuery, retryToken, semantic]);

  const resultSummary = loading
    ? "Searching evidence…"
    : error
      ? "Search error"
      : queryTooShort
        ? SEARCH_MIN_QUERY_HINT
        : hasSearched
          ? `${results.length} ${results.length === 1 ? "match" : "matches"} ${
              resultMode === "semantic" ? "(semantic) " : ""
            }for “${trimmedQuery}”`
          : "Search captured evidence across every bot.";

  return (
    <section className="h-full overflow-y-auto bg-[#f4f7fa]" aria-label="Evidence search">
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-[#f4f7fa]/95 px-4 py-4 backdrop-blur md:px-7">
        <p className="font-mono text-[11px] uppercase tracking-[0.24em] text-[#197e74]">
          Full-text evidence
        </p>
        <h1 className="font-[Arial_Narrow,Arial,sans-serif] text-3xl font-black tracking-tight text-[#172033]">
          Global search
        </h1>

        <div className="mt-4 flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 shadow-sm focus-within:border-[#197e74] focus-within:ring-2 focus-within:ring-[#197e74]/25">
          <SearchIcon aria-hidden="true" className="h-4 w-4 shrink-0 text-slate-400" />
          <input
            ref={inputRef}
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search evidence"
            aria-describedby="search-result-summary"
            placeholder="Search message content…  (press / to focus)"
            className="w-full bg-transparent text-sm text-slate-900 outline-none placeholder:text-slate-400"
          />
          <kbd className="hidden shrink-0 rounded border border-slate-300 bg-slate-50 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 md:inline">
            /
          </kbd>
          <button
            type="button"
            aria-pressed={semantic}
            aria-label="Semantic search"
            onClick={() => setSemantic((value) => !value)}
            className={`flex shrink-0 items-center gap-1 rounded border px-2 py-1 font-mono text-[10px] font-bold uppercase tracking-wide transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] ${
              semantic
                ? "border-[#197e74] bg-[#197e74]/10 text-[#12645c]"
                : "border-slate-300 bg-white text-slate-500 hover:bg-slate-50"
            }`}
          >
            <Sparkles aria-hidden="true" className="h-3 w-3" /> Semantic
          </button>
        </div>
        <p
          id="search-result-summary"
          role="status"
          aria-live="polite"
          className="mt-2 text-sm text-slate-600"
        >
          {resultSummary}
        </p>
      </div>

      <div className="mx-auto flex max-w-5xl flex-col gap-3 p-4 md:p-7">
        {!loading && !error && notice && (
          <p
            role="status"
            className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900"
          >
            {notice}
          </p>
        )}

        {loading && (
          <div className="space-y-3" aria-hidden="true">
            {[0, 1, 2].map((index) => (
              <div
                key={index}
                className="animate-pulse rounded-lg border border-slate-200 bg-white p-4 motion-reduce:animate-none"
              >
                <div className="flex items-center gap-2">
                  <div className="h-7 w-7 rounded-full bg-slate-200" />
                  <div className="h-3 w-28 rounded bg-slate-200" />
                  <div className="ml-auto h-3 w-16 rounded bg-slate-200" />
                </div>
                <div className="mt-3 h-3 w-full rounded bg-slate-200" />
                <div className="mt-2 h-3 w-2/3 rounded bg-slate-200" />
              </div>
            ))}
          </div>
        )}

        {!loading && error && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{error}</span>
            <button
              type="button"
              onClick={() => setRetryToken((value) => value + 1)}
              className="flex items-center gap-1.5 rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
            >
              <RefreshCw className="h-3.5 w-3.5" /> Retry
            </button>
          </div>
        )}

        {!loading && !error && !hasSearched && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
            <SearchIcon aria-hidden="true" className="mx-auto h-7 w-7 text-[#197e74]" />
            <h2 className="mt-3 font-bold text-slate-900">Search every captured chat</h2>
            <p className="mt-1 text-sm text-slate-600">
              {queryTooShort
                ? `${SEARCH_MIN_QUERY_HINT} to search captured evidence.`
                : <>Results are ranked newest first. Press <kbd className="font-mono">/</kbd> to jump to the box.</>}
            </p>
          </div>
        )}

        {!loading && !error && hasSearched && results.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
            <h2 className="font-bold text-slate-900">No matches</h2>
            <p className="mt-1 text-sm text-slate-600">
              No captured messages match “{trimmedQuery}”. Try a shorter or broader term.
            </p>
          </div>
        )}

        {!loading &&
          !error &&
          results.map((row) => {
            const senderKey = row.sender_key || "unknown";
            const time = formatTime(row.created_at);
            return (
              <article
                key={row.id}
                className="rounded-lg border border-slate-200 bg-white shadow-[0_1px_0_rgba(23,32,51,0.04)]"
              >
                <div className="flex flex-wrap items-center gap-2 border-b border-slate-100 px-4 py-3">
                  <span
                    title={senderKey}
                    aria-label={`Sender ${senderKey}`}
                    className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full border font-mono text-[10px] font-bold ${chipTone(senderKey)}`}
                  >
                    {senderInitials(row.sender_key)}
                  </span>
                  <span className="truncate font-mono text-xs font-semibold text-slate-700">
                    {senderKey}
                  </span>
                  {row.sender_pseudonym && row.sender_pseudonym !== senderKey && (
                    <span className="hidden truncate font-mono text-[10px] text-slate-400 md:inline">
                      {row.sender_pseudonym}
                    </span>
                  )}
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-slate-600">
                    {row.media_type || "text"}
                  </span>
                  <span
                    data-testid="result-mode"
                    className={`rounded border px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-wide ${
                      resultMode === "semantic"
                        ? "border-[#197e74]/30 bg-[#197e74]/10 text-[#12645c]"
                        : "border-slate-300 bg-white text-slate-500"
                    }`}
                  >
                    {resultMode === "semantic" ? "Semantic" : "FTS"}
                  </span>
                  <time
                    dateTime={row.created_at || undefined}
                    title={time.title}
                    className="ml-auto whitespace-nowrap font-mono text-[11px] text-slate-500"
                  >
                    {time.label}
                  </time>
                </div>

                <div className="px-4 py-3">
                  <HighlightedSnippet snippet={row.snippet} />
                </div>

                <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-4 py-2">
                  <span className="truncate font-mono text-[10px] text-slate-400">
                    message {row.id.slice(0, 8)}
                    {row.credential_id ? ` · chat ${row.credential_id.slice(0, 8)}` : ""}
                  </span>
                  <button
                    type="button"
                    disabled={!row.credential_id}
                    onClick={() => row.credential_id && onOpenChat(row.credential_id)}
                    className="flex items-center gap-1.5 rounded-md bg-[#172033] px-3 py-1.5 text-xs font-bold text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74] disabled:cursor-not-allowed disabled:opacity-40"
                  >
                    <MessageSquareText className="h-3.5 w-3.5" /> Open chat
                  </button>
                </div>
              </article>
            );
          })}
      </div>
    </section>
  );
}
