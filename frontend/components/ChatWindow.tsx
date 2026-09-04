"use client";

import { useEffect, useState, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { useAuth } from "@/lib/auth";
import { ChevronDown, ChevronUp, FileDown, Globe2, LucideSend, Server, TerminalSquare } from "lucide-react";
import type { Credential, InfrastructureContext } from "@/app/page";

export default function ChatWindow({ credential }: { credential: Credential | null }) {
    const { session } = useAuth();
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
            setMessages([]);
            
            // Gate: must be authenticated to read evidence
            if (!session) {
                setAuthError("Sign in required to view exfiltrated messages.");
                setLoading(false);
                return;
            }
            
            // Authenticated query to evidence_redacted view (truncated + masked content)
            const { data, error } = await supabase
                .from("evidence_redacted")
                .select("*")
                .eq("credential_id", credentialId)
                .order("created_at", { ascending: true })
                .limit(200);

            if (cancelled) return;

            if (error) {
                const code = (error as { code?: string; status?: number }).code;
                const status = (error as { status?: number }).status;
                const isAuth =
                    status === 401 ||
                    status === 403 ||
                    code === "PGRST301" ||
                    code === "42501" ||
                    /permission|denied|jwt|auth|rls/i.test(error.message);
                setAuthError(
                    isAuth
                        ? "Sign in required to view exfiltrated messages."
                        : `Failed to load messages: ${error.message}`
                );
                setLoading(false);
                return;
            }

            if (data) setMessages((data as ChatMessage[]));
            setLoading(false);
        }

        fetchMsgs();
        // NO realtime subscription - poll-based refresh only for redacted view

        return () => {
            cancelled = true;
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
            <div className="flex-1 overflow-y-auto p-4 space-y-2">
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
            </div>
        </div>
    );
}

function collectInfrastructureEndpoints(context: InfrastructureContext | undefined): string[] {
    if (!context) return [];
    const endpoints: string[] = [];
    if (context.api_urls && Array.isArray(context.api_urls)) {
        endpoints.push(...context.api_urls.filter((u): u is string => typeof u === 'string' && u.length > 0));
    }
    if (context.webhook_urls && Array.isArray(context.webhook_urls)) {
        endpoints.push(...context.webhook_urls.filter((u): u is string => typeof u === 'string' && u.length > 0));
    }
    return [...new Set(endpoints)];
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
