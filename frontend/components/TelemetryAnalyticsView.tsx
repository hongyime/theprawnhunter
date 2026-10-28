"use client";

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { Check, Copy, Link2, Network, Radar, Search, WalletCards } from "lucide-react";
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
    network_domain: "bg-accent/10 text-accent-strong border-accent/30",
    canonical_url: "bg-paper text-slate-700 border-slate-200",
    wallet_address: "bg-accent/10 text-accent-strong border-accent/30",
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

    const displayBotId = credential?.meta?.bot_id || credential?.id.slice(0, 8);
    const tableColumnCount = scope === "global" ? 5 : 4;

    if (isCredentialScope && !credential) {
        return (
            <section className="flex-1 overflow-y-auto bg-paper text-ink">
                <HeaderShell
                    title="Bot Intel"
                    badge="no bot selected"
                />
                <div className="flex min-h-[calc(100vh-5rem)] items-center justify-center px-4">
                    <div className="max-w-sm rounded-lg border border-slate-200 bg-white p-5 text-center shadow-[0_1px_0_rgba(23,32,51,0.04)]">
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
        <section className="flex-1 overflow-y-auto bg-paper text-ink">
            <HeaderShell
                title={scope === "global" ? "All Telemetry" : "Bot Intel"}
                badge={scope === "global" ? `latest ${limit} rows` : credential?.source || "selected bot"}
                meta={
                    scope === "credential" ? (
                        <div className="flex items-center gap-2">
                            <span className="rounded-md bg-slate-200 px-1.5 py-0.5 text-xs font-mono uppercase text-slate-600">
                                {credential?.source}
                            </span>
                            <span className="text-xs font-mono text-slate-400">ID: {displayBotId}</span>
                        </div>
                    ) : undefined
                }
            />

            <div className="mx-auto flex w-full max-w-6xl flex-col gap-4 p-4 md:p-7">
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

                <div className="rounded-lg border border-slate-200 bg-white shadow-[0_1px_0_rgba(23,32,51,0.04)]">
                    <div className="flex flex-col gap-2 border-b border-slate-200 p-3 lg:flex-row lg:items-center">
                        <div className="relative min-w-0 flex-1">
                            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
                            <input
                                value={query}
                                onChange={(event) => setQuery(event.currentTarget.value)}
                                placeholder="Search indicator values"
                                className="h-9 w-full rounded-md border border-slate-200 bg-white pl-9 pr-3 text-sm text-slate-800 outline-none transition-colors placeholder:text-slate-400 focus:border-accent"
                            />
                        </div>
                        <div className="flex overflow-x-auto rounded-md bg-paper p-1">
                            {(["all", ...INDICATOR_TYPES] as IndicatorFilter[]).map((type) => (
                                <button
                                    key={type}
                                    type="button"
                                    onClick={() => setFilter(type)}
                                    className={`whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                                        filter === type
                                            ? "bg-white text-ink shadow-[0_1px_0_rgba(23,32,51,0.04)]"
                                            : "text-slate-500 hover:text-ink"
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
                                        <td className="px-4 py-8 text-center text-red-700" colSpan={tableColumnCount}>
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
                                            <span className={`inline-flex rounded-md border px-2 py-1 text-xs font-semibold ${TYPE_STYLES[indicator.indicator_type]}`}>
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
                                                className="inline-flex h-8 w-8 items-center justify-center rounded-md border border-slate-200 text-slate-500 transition-colors hover:border-accent hover:text-accent-strong"
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
    title,
    badge,
    meta,
}: {
    title: string;
    badge: string;
    meta?: ReactNode;
}) {
    return (
        <div className="sticky top-0 z-10 border-b border-slate-200 bg-paper/95 px-4 py-3 backdrop-blur">
            <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                    <h1 className="sr-only">{title}</h1>
                    {meta && <div>{meta}</div>}
                </div>
                <span className="shrink-0 rounded-md border border-slate-200 bg-slate-50 px-2 py-1 text-xs font-mono text-slate-500">
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
        <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-[0_1px_0_rgba(23,32,51,0.04)]">
            <div className="flex items-center justify-between gap-2 text-slate-500">
                <span className="text-xs font-semibold uppercase">{label}</span>
                {icon}
            </div>
            <div className="mt-1 font-mono text-lg font-semibold text-ink">
                {value.toLocaleString()}
            </div>
        </div>
    );
}
