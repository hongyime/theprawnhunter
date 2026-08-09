"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { BarChart3, Check, Copy, Link2, Network, Radar, Search, WalletCards } from "lucide-react";
import { supabase } from "@/lib/supabase";
import type { Credential } from "@/app/page";

type IndicatorType = "network_domain" | "canonical_url" | "wallet_address";
type IndicatorFilter = "all" | IndicatorType;

type TelemetryScope = "global" | "credential";

type TelemetryIndicator = {
    id: string;
    credential_id: string | null;
    telegram_msg_id?: number | null;
    message_id?: string | null;
    indicator_type: IndicatorType;
    indicator_value: string;
    first_seen_at: string | null;
    meta?: Record<string, unknown> | null;
    raw_context?: Record<string, unknown> | null;
};

const INDICATOR_TYPES: IndicatorType[] = ["network_domain", "canonical_url", "wallet_address"];

const TYPE_LABELS: Record<IndicatorType, string> = {
    network_domain: "Network Domain",
    canonical_url: "Canonical URL",
    wallet_address: "Blockchain Wallet",
};

const TYPE_STYLES: Record<IndicatorType, string> = {
    network_domain: "bg-cyan-50 text-cyan-700 border-cyan-200",
    canonical_url: "bg-indigo-50 text-indigo-700 border-indigo-200",
    wallet_address: "bg-emerald-50 text-emerald-700 border-emerald-200",
};

export default function TelemetryAnalyticsView({
    scope,
    credential = null,
    limit = 100,
}: {
    scope: TelemetryScope;
    credential?: Credential | null;
    limit?: number;
}) {
    const [indicators, setIndicators] = useState<TelemetryIndicator[]>([]);
    const [counts, setCounts] = useState<Record<IndicatorType, number>>({
        network_domain: 0,
        canonical_url: 0,
        wallet_address: 0,
    });
    const [filter, setFilter] = useState<IndicatorFilter>("all");
    const [query, setQuery] = useState("");
    const [copiedId, setCopiedId] = useState<string | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const isCredentialScope = scope === "credential";
    const credentialId = credential?.id;

    useEffect(() => {
        if (isCredentialScope && !credentialId) {
            return;
        }

        async function fetchTelemetry() {
            setIsLoading(true);
            setError(null);

            const recordsQuery = supabase
                .from("telemetry_indicators")
                .select("*")
                .order("first_seen_at", { ascending: false })
                .limit(limit);

            const scopedRecordsQuery =
                isCredentialScope && credentialId
                    ? recordsQuery.eq("credential_id", credentialId)
                    : recordsQuery;

            const countQueries = INDICATOR_TYPES.map((indicatorType) => {
                const countQuery = supabase
                    .from("telemetry_indicators")
                    .select("id", { count: "exact", head: true })
                    .eq("indicator_type", indicatorType);

                return isCredentialScope && credentialId
                    ? countQuery.eq("credential_id", credentialId)
                    : countQuery;
            });

            const [recordsResult, ...countResults] = await Promise.all([
                scopedRecordsQuery,
                ...countQueries,
            ]);

            if (recordsResult.error) {
                setError(recordsResult.error.message);
                setIndicators([]);
                setIsLoading(false);
                return;
            }

            const nextIndicators = (recordsResult.data || []) as TelemetryIndicator[];
            const nextCounts: Record<IndicatorType, number> = {
                network_domain: 0,
                canonical_url: 0,
                wallet_address: 0,
            };
            INDICATOR_TYPES.forEach((indicatorType, index) => {
                nextCounts[indicatorType] =
                    countResults[index].count ??
                    nextIndicators.filter((row) => row.indicator_type === indicatorType).length;
            });

            setIndicators(nextIndicators);
            setCounts(nextCounts);
            setIsLoading(false);
        }

        fetchTelemetry();
    }, [credentialId, isCredentialScope, limit]);

    const filteredIndicators = useMemo(() => {
        const normalizedQuery = query.trim().toLowerCase();
        return indicators.filter((indicator) => {
            const matchesType = filter === "all" || indicator.indicator_type === filter;
            const matchesQuery =
                !normalizedQuery ||
                indicator.indicator_value.toLowerCase().includes(normalizedQuery) ||
                indicator.indicator_type.toLowerCase().includes(normalizedQuery) ||
                indicator.credential_id?.toLowerCase().includes(normalizedQuery);
            return matchesType && matchesQuery;
        });
    }, [filter, indicators, query]);

    async function copyIndicator(indicator: TelemetryIndicator) {
        await navigator.clipboard.writeText(indicator.indicator_value);
        setCopiedId(indicator.id);
        window.setTimeout(() => setCopiedId(null), 1200);
    }

    const displayName = credential?.meta?.bot_username
        ? `@${credential.meta.bot_username}`
        : credential?.meta?.chat_title || "Unknown Bot";
    const displayBotId = credential?.meta?.bot_id || credential?.id.slice(0, 8);
    const tableColumnCount = scope === "global" ? 5 : 4;

    if (isCredentialScope && !credential) {
        return (
            <section className="flex-1 overflow-y-auto bg-slate-100">
                <HeaderShell
                    icon={<Radar className="h-5 w-5 text-cyan-600" />}
                    title="Bot Intel"
                    subtitle="Select a bot to inspect telemetry indicators for that credential."
                    badge="no bot selected"
                />
                <div className="flex min-h-[calc(100vh-5rem)] items-center justify-center px-4">
                    <div className="max-w-sm rounded border border-slate-200 bg-white p-5 text-center shadow-sm">
                        <Radar className="mx-auto h-8 w-8 text-slate-400" />
                        <h2 className="mt-3 text-sm font-semibold text-slate-800">No bot selected</h2>
                        <p className="mt-1 text-sm text-slate-500">
                            Choose a discovered bot from the sidebar to load its scoped telemetry.
                        </p>
                    </div>
                </div>
            </section>
        );
    }

    return (
        <section className="flex-1 overflow-y-auto bg-slate-100 text-slate-900">
            <HeaderShell
                icon={
                    scope === "global" ? (
                        <BarChart3 className="h-5 w-5 text-cyan-600" />
                    ) : (
                        <Radar className="h-5 w-5 text-cyan-600" />
                    )
                }
                title={scope === "global" ? "All Telemetry" : "Bot Intel"}
                subtitle={
                    scope === "global"
                        ? "All bots, latest extracted telemetry indicators."
                        : displayName
                }
                badge={scope === "global" ? `latest ${limit} rows` : credential?.source || "selected bot"}
                meta={
                    scope === "credential" ? (
                        <div className="flex items-center gap-2">
                            <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-mono uppercase text-slate-600">
                                {credential?.source}
                            </span>
                            <span className="text-xs font-mono text-slate-400">ID: {displayBotId}</span>
                        </div>
                    ) : undefined
                }
            />

            <div className="space-y-3 p-3 md:p-4">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                    <MetricCard
                        icon={<Network className="h-4 w-4" />}
                        label="Domains"
                        value={counts.network_domain}
                    />
                    <MetricCard
                        icon={<Link2 className="h-4 w-4" />}
                        label="URLs"
                        value={counts.canonical_url}
                    />
                    <MetricCard
                        icon={<WalletCards className="h-4 w-4" />}
                        label="Wallets"
                        value={counts.wallet_address}
                    />
                </div>

                <div className="rounded border border-slate-200 bg-white shadow-sm">
                    <div className="flex flex-col gap-2 border-b border-slate-200 p-3 lg:flex-row lg:items-center">
                        <div className="relative min-w-0 flex-1">
                            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                            <input
                                value={query}
                                onChange={(event) => setQuery(event.currentTarget.value)}
                                placeholder="Search indicator values"
                                className="h-9 w-full rounded border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-800 outline-none transition-colors placeholder:text-slate-400 focus:border-cyan-500"
                            />
                        </div>
                        <div className="flex overflow-x-auto rounded bg-slate-100 p-1">
                            {(["all", ...INDICATOR_TYPES] as IndicatorFilter[]).map((type) => (
                                <button
                                    key={type}
                                    type="button"
                                    onClick={() => setFilter(type)}
                                    className={`whitespace-nowrap rounded px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                                        filter === type
                                            ? "bg-white text-slate-900 shadow-sm"
                                            : "text-slate-500 hover:text-slate-800"
                                    }`}
                                >
                                    {type === "all" ? "All" : TYPE_LABELS[type]}
                                </button>
                            ))}
                        </div>
                    </div>

                    <div className="overflow-x-auto">
                        <table className="w-full min-w-[720px] table-fixed text-left text-sm">
                            <thead className="bg-slate-50 text-xs uppercase text-slate-500">
                                <tr>
                                    {scope === "global" && <th className="w-32 px-4 py-3">Bot</th>}
                                    <th className="w-44 px-4 py-3">Type</th>
                                    <th className="px-4 py-3">Indicator Value</th>
                                    <th className="w-44 px-4 py-3">First Seen</th>
                                    <th className="w-16 px-4 py-3 text-right">Copy</th>
                                </tr>
                            </thead>
                            <tbody className="divide-y divide-slate-100">
                                {isLoading && (
                                    <tr>
                                        <td className="px-4 py-8 text-center text-slate-500" colSpan={tableColumnCount}>
                                            Loading telemetry indicators...
                                        </td>
                                    </tr>
                                )}
                                {!isLoading && error && (
                                    <tr>
                                        <td className="px-4 py-8 text-center text-rose-600" colSpan={tableColumnCount}>
                                            {error}
                                        </td>
                                    </tr>
                                )}
                                {!isLoading && !error && filteredIndicators.length === 0 && (
                                    <tr>
                                        <td className="px-4 py-8 text-center text-slate-500" colSpan={tableColumnCount}>
                                            No indicators match the current filter.
                                        </td>
                                    </tr>
                                )}
                                {!isLoading && !error && filteredIndicators.map((indicator) => (
                                    <tr key={indicator.id} className="hover:bg-slate-50">
                                        {scope === "global" && (
                                            <td className="px-4 py-3">
                                                <span className="font-mono text-xs text-slate-500">
                                                    {indicator.credential_id ? indicator.credential_id.slice(0, 8) : "unknown"}
                                                </span>
                                            </td>
                                        )}
                                        <td className="px-4 py-3">
                                            <span className={`inline-flex rounded border px-2 py-1 text-xs font-semibold ${TYPE_STYLES[indicator.indicator_type]}`}>
                                                {TYPE_LABELS[indicator.indicator_type]}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3">
                                            <span className="block truncate font-mono text-xs text-slate-800" title={indicator.indicator_value}>
                                                {indicator.indicator_value}
                                            </span>
                                        </td>
                                        <td className="px-4 py-3 text-xs text-slate-500">
                                            {indicator.first_seen_at
                                                ? new Date(indicator.first_seen_at).toLocaleString()
                                                : "Unknown"}
                                        </td>
                                        <td className="px-4 py-3 text-right">
                                            <button
                                                type="button"
                                                onClick={() => copyIndicator(indicator)}
                                                className="inline-flex h-8 w-8 items-center justify-center rounded border border-slate-200 text-slate-500 transition-colors hover:border-cyan-400 hover:text-cyan-700"
                                                title="Copy indicator value"
                                                aria-label="Copy indicator value"
                                            >
                                                {copiedId === indicator.id ? (
                                                    <Check className="h-4 w-4" />
                                                ) : (
                                                    <Copy className="h-4 w-4" />
                                                )}
                                            </button>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </section>
    );
}

function HeaderShell({
    icon,
    title,
    subtitle,
    badge,
    meta,
}: {
    icon: ReactNode;
    title: string;
    subtitle: string;
    badge: string;
    meta?: ReactNode;
}) {
    return (
        <div className="sticky top-0 z-10 border-b bg-white/90 px-4 py-3 shadow-sm backdrop-blur">
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <h1 className="flex items-center gap-2 text-base font-semibold text-slate-800">
                        {icon}
                        <span className="truncate">{title}</span>
                    </h1>
                    <p className="mt-0.5 truncate text-sm text-slate-500">{subtitle}</p>
                    {meta && <div className="mt-1">{meta}</div>}
                </div>
                <span className="shrink-0 rounded border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-mono text-slate-500">
                    {badge}
                </span>
            </div>
        </div>
    );
}

function MetricCard({
    icon,
    label,
    value,
}: {
    icon: ReactNode;
    label: string;
    value: number;
}) {
    return (
        <div className="rounded border border-slate-200 bg-white px-3 py-2 shadow-sm">
            <div className="flex items-center justify-between gap-2 text-slate-500">
                <span className="text-xs font-semibold uppercase">{label}</span>
                {icon}
            </div>
            <div className="mt-1 font-mono text-xl font-semibold text-slate-900">
                {value.toLocaleString()}
            </div>
        </div>
    );
}
