"use client";

import { useEffect, useState, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { useAuth } from "@/lib/auth";
import { ChevronDown, ChevronUp, FileDown, Globe2, LucideSend, Server, TerminalSquare, LogIn, LogOut } from "lucide-react";
import type { Credential, InfrastructureContext } from "@/app/page";

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8011";

export default function ChatWindow({ credential }: { credential: Credential | null }) {
    const { user, session, loading: authLoading, error: authCtxError, signIn, signOut } = useAuth();
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [isProfileExpanded, setIsProfileExpanded] = useState(true);
    const [loading, setLoading] = useState(false);
    const [authError, setAuthError] = useState<string | null>(null);
    const bottomRef = useRef<HTMLDivElement>(null);
    const credentialId = credential?.id;

    useEffect(() => {
        if (!credentialId) return;

        let cancelled = false;

        async function fetchMsgs() {
            setLoading(true);
            setAuthError(null);
            setMessages([]); // clear stale state on credential change / auth failure
            
            // Gate: must be authenticated to read evidence
            if (!session) {
                setAuthError("Sign in required to view exfiltrated messages.");
                setLoading(false);
                return;
            }
            
            // Authenticated query to evidence_redacted view (truncated content, no sensitive fields)
            const { data, error } = await supabase
                .from("evidence_redacted")
                .select("*")
                .eq("credential_id", credentialId)
                .order("telegram_msg_id", { ascending: false })
                .limit(200);

            if (cancelled) return;

            if (error) {
                // RLS blocks unauthenticated SELECT — 401/403/PGRST come back as errors.
                const code = (error as { code?: string; status?: number }).code;
                const status = (error as { status?: number }).status;
                const isAuth =
                    status === 401 ||
                    status === 403 ||
                    code === "PGRST301" || // JWT expired / missing
                    code === "42501" ||     // insufficient privilege
                    /permission|denied|jwt|auth|rls/i.test(error.message);
                setAuthError(
                    isAuth
                        ? "Sign in required to view exfiltrated messages."
                        : `Failed to load messages: ${error.message}`
                );
                setLoading(false);
                return;
            }

            // Reverse to show oldest-first in the chat view
            if (data) setMessages((data as ChatMessage[]).reverse());
            setLoading(false);
        }

        fetchMsgs();

        const channel = supabase
            .channel(`chat-${credentialId}`)
            .on(
                "postgres_changes",
                {
                    event: "INSERT",
                    schema: "public",
                    table: "exfiltrated_messages",
                    filter: `credential_id=eq.${credentialId}`,
                },
                (payload) => {
                    setMessages((prev) => [...prev, payload.new as ChatMessage]);
                }
            )
            .subscribe((status) => {
                if (status === "CHANNEL_ERROR" || status === "TIMED_OUT") {
                    setAuthError("Realtime subscription failed — sign in required.");
                }
            });

        return () => {
            cancelled = true;
            supabase.removeChannel(channel);
        };
    }, [credentialId, session]);

    useEffect(() => {
        bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [messages]);

    if (!credential) {
        return (
            <div className="flex-1 flex items-center justify-center bg-slate-200 text-slate-600">
                Select a chat to view exfiltrated messages
            </div>
        );
    }

    const displayName = credential.meta?.bot_username
        ? `@${credential.meta.bot_username}`
        : credential.meta?.chat_title || "Unknown Bot";
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
                        </div>
                    </div>
                </div>
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
                            <div className="grid grid-cols-3 gap-3 border-t border-slate-200 px-3 py-3 text-xs">
                                <div className="min-w-0 space-y-1">
                                    <div className="flex items-center gap-1.5 font-semibold uppercase text-slate-500">
                                        <Globe2 className="h-3.5 w-3.5" />
                                        Gateway
                                    </div>
                                    <div className="truncate font-mono text-slate-800" title={gatewayTelemetry?.configured_webhook_url || undefined}>
                                        {gatewayTelemetry?.configured_webhook_url || "No remote endpoint"}
                                    </div>
                                    {gatewayTelemetry?.resolved_ip_address && (
                                        <div className="font-mono text-slate-500">
                                            {gatewayTelemetry.resolved_ip_address}
                                        </div>
                                    )}
                                </div>
                                <div className="min-w-0 space-y-2">
                                    <div className="flex items-center gap-1.5 font-semibold uppercase text-slate-500">
                                        <TerminalSquare className="h-3.5 w-3.5" />
                                        Commands
                                    </div>
                                    <div className="flex flex-wrap gap-1">
                                        {commands.length > 0 ? commands.slice(0, 8).map((command) => (
                                            <span key={command} className="rounded bg-slate-900 px-1.5 py-0.5 font-mono text-[10px] text-white">
                                                /{command.replace(/^\//, "")}
                                            </span>
                                        )) : (
                                            <span className="text-slate-500">No commands indexed</span>
                                        )}
                                    </div>
                                </div>
                                <div className="min-w-0 space-y-2">
                                    <div className="font-semibold uppercase text-slate-500">
                                        Repository Constants
                                    </div>
                                    <div className="flex flex-wrap gap-1">
                                        {infrastructureEndpoints.length > 0 ? infrastructureEndpoints.slice(0, 6).map((endpoint) => (
                                            <span key={endpoint} className="max-w-full truncate rounded bg-cyan-50 px-1.5 py-0.5 font-mono text-[10px] text-cyan-800" title={endpoint}>
                                                {endpoint}
                                            </span>
                                        )) : (
                                            <span className="text-slate-500">No adjacent endpoints indexed</span>
                                        )}
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                )}
            </div>

            <div className="flex-1 overflow-y-auto p-4 flex flex-col space-y-3">
                {authError && (
                    <div className="self-center max-w-md rounded-md border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800 shadow-sm">
                        <div className="font-semibold mb-1">Authentication required</div>
                        <div className="text-xs">{authError}</div>
                    </div>
                )}
                {loading && !authError && (
                    <div className="self-center text-xs text-slate-500 italic">Loading messages…</div>
                )}
                {!loading && !authError && messages.length === 0 && (
                    <div className="self-center text-xs text-slate-500 italic">No messages captured for this bot yet.</div>
                )}
                {messages.map((msg) => (
                    <div
                        key={msg.id}
                        className={`flex flex-col max-w-[70%] p-2 rounded-lg shadow-sm ${msg.sender_name === "me" || msg.sender_name?.toLowerCase().includes("bot")
                            ? "self-end bg-[#DCF8C6] rounded-tr-none"
                            : "self-start bg-white rounded-tl-none"
                            }`}
                    >
                        <span className="text-xs font-bold text-sky-600 mb-0.5">
                            {msg.sender_name || "Unknown"}
                        </span>
                        <MediaRenderer msg={msg} />
                        <p className="text-sm text-slate-800 whitespace-pre-wrap leading-snug break-all">
                            {msg.content}
                        </p>
                        <span className="text-[10px] text-slate-400 self-end mt-1">
                            {msg.created_at
                                ? new Date(msg.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                                : ""}
                        </span>
                    </div>
                ))}
                <div ref={bottomRef} />
            </div>

            {/* Input area (ReadOnly) */}
            <div className="p-3 bg-white border-t flex items-center gap-2 text-slate-400 text-sm italic justify-center">
                <LucideSend className="w-4 h-4" />
                <span>Read-only Mode (Exfiltrated Data)</span>
            </div>
        </div>
    );
}

type ChatMessage = {
    id: string;
    sender_name?: string | null;
    content?: string | null;
    created_at?: string | null;
    media_type?: string | null;
    file_meta?: Record<string, unknown> | null;
};

function MediaRenderer({ msg }: { msg: ChatMessage }) {
    const mediaType = msg.media_type;
    const fileId = (msg.file_meta as Record<string, unknown>)?.file_id;

    if (!mediaType || mediaType === "text" || !fileId) return null;

    const mediaUrl = `${API_BASE_URL}/media/${msg.id}`;

    if (mediaType === "photo") {
        return (
            <img
                src={mediaUrl}
                alt="Attached photo"
                className="rounded-md max-w-full max-h-64 object-cover mb-1.5 cursor-pointer"
                loading="lazy"
                onClick={() => window.open(mediaUrl, "_blank")}
                onError={(e) => {
                    (e.target as HTMLImageElement).style.display = "none";
                }}
            />
        );
    }

    if (mediaType === "video") {
        return (
            <video
                src={mediaUrl}
                controls
                className="rounded-md max-w-full max-h-64 mb-1.5"
                preload="metadata"
            />
        );
    }

    if (mediaType === "audio") {
        return (
            <audio src={mediaUrl} controls className="w-full mb-1.5" preload="metadata" />
        );
    }

    // document / other
    const fileName = (msg.file_meta as Record<string, unknown>)?.file_name;
    return (
        <a
            href={mediaUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 bg-slate-100 rounded-md px-3 py-2 mb-1.5 text-sm text-sky-700 hover:bg-slate-200 transition-colors"
        >
            <FileDown className="h-4 w-4 shrink-0" />
            <span className="truncate">{(fileName as string) || "Download document"}</span>
        </a>
    );
}

function collectInfrastructureEndpoints(context?: InfrastructureContext): string[] {
    if (!context) {
        return [];
    }

    const endpoints = new Set<string>();
    context.co_located_endpoints?.forEach((value) => {
        if (value) endpoints.add(value);
    });

    Object.entries(context).forEach(([key, value]) => {
        if (!/(?:_HOST|_DOMAIN|_URL|_IP|HOST|DOMAIN|URL|IP)$/i.test(key)) {
            return;
        }
        if (typeof value === "string" && value.trim()) {
            endpoints.add(value.trim());
        }
        if (Array.isArray(value)) {
            value.forEach((item) => {
                if (typeof item === "string" && item.trim()) {
                    endpoints.add(item.trim());
                }
            });
        }
    });

    return Array.from(endpoints).sort();
}
