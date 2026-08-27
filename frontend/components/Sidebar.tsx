"use client";

import { useEffect, useState, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { BarChart3, MessageSquareText, LucideTarget, Radar } from "lucide-react";
import type { Credential, DashboardView } from "@/app/page";

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
    const [credentials, setCredentials] = useState<Credential[]>([]);
    // Use ref to access current credentials in realtime callback without causing re-subscription
    const credentialsRef = useRef<Credential[]>([]);

    // Keep ref in sync with state
    useEffect(() => {
        credentialsRef.current = credentials;
    }, [credentials]);

    useEffect(() => {
        async function fetchCreds() {
            console.log("[Sidebar] Fetching credentials...");

            try {
                // Fetch credentials. After migration 004 the public view exposes
                // confidence_score and chat_member_count as real INT columns.
                // If the view is on the old schema (pre-migration), fall back
                // to the legacy SELECT so the app keeps working during rollout.
                let creds: Credential[] | null = null;
                let error: { message: string } | null = null;

                const { data: dataNew, error: errNew } = await supabase
                    .from("discovered_credentials_public")
                    .select("id, created_at, source, meta, confidence_score, chat_member_count")
                    .not("id", "is", null)
                    .order("confidence_score", { ascending: false, nullsFirst: false })
                    .order("created_at", { ascending: false })
                    .limit(500);

                if (errNew && /confidence_score|chat_member_count/.test(errNew.message)) {
                    // View is on old schema — migration 004 not applied yet.
                    console.warn("[Sidebar] migration 004 not applied; falling back");
                    const { data: dataOld, error: errOld } = await supabase
                        .from("discovered_credentials_public")
                        .select("id, created_at, source, meta")
                        .not("id", "is", null)
                        .order("created_at", { ascending: false })
                        .limit(500);
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
            }
        }

        fetchCreds();

        // Realtime subscription - when new message arrives, check if it's a new credential
        const channel = supabase
            .channel('schema-db-changes')
            .on(
                'postgres_changes',
                {
                    event: 'INSERT',
                    schema: 'public',
                    table: 'exfiltrated_messages',
                },
                async (payload) => {
                    const newMsg = payload.new as { credential_id: string };
                    const credId = newMsg.credential_id;

                    // Use ref to check current credentials without causing re-subscription
                    const exists = credentialsRef.current.some(c => c.id === credId);

                    if (!exists) {
                        // Fetch via the safe public view — anon key cannot SELECT raw table
                        const { data: credData } = await supabase
                            .from("discovered_credentials_public")
                            .select("*")
                            .eq("id", credId)
                            .single();

                        if (credData) {
                            setCredentials((prev) => [credData, ...prev]);
                        }
                    }
                }
            )
            .subscribe()

        return () => {
            supabase.removeChannel(channel);
        }
    }, []); // ✅ Empty dependency array - runs once on mount

    return (
        <div className="w-full h-full flex flex-col bg-slate-50 overflow-y-auto border-r">
            <div className="p-4 border-b bg-white sticky top-0 z-10">
                <h2 className="font-bold text-lg flex items-center gap-2 text-slate-800">
                    <LucideTarget className="text-red-600" /> Discovered Bots
                </h2>
                <div className="mt-3 grid grid-cols-3 gap-1 rounded-md bg-slate-100 p-1">
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
                </div>
            </div>
            <div className="flex flex-col">
                {credentials.map((cred) => (
                    <button
                        key={cred.id}
                        onClick={() => onSelect(cred)}
                        className={`p-4 border-b text-left hover:bg-slate-100 transition-colors ${selected?.id === cred.id ? "bg-blue-50 border-l-4 border-l-blue-500" : ""
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
                            <ConfidenceBadge score={cred.confidence_score ?? null} />
                            {typeof cred.chat_member_count === "number" && cred.chat_member_count > 1 && (
                                <span className="bg-blue-50 text-blue-700 px-1 py-0.5 rounded text-[10px] font-mono" title={`${cred.chat_member_count} members`}>
                                    👥{cred.chat_member_count >= 1000 ? `${Math.round(cred.chat_member_count / 1000)}k` : cred.chat_member_count}
                                </span>
                            )}
                            <span className="font-mono text-xs opacity-70 truncate">ID: {cred.meta?.bot_id || cred.id.slice(0, 8)}</span>
                        </div>
                    </button>
                ))}
            </div>
        </div>
    );
}
