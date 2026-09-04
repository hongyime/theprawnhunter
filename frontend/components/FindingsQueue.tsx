"use client";

import { useEffect, useMemo, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Loader2,
  MessageSquareText,
  Radar,
  RefreshCw,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";

import { supabase } from "@/lib/supabase";
import type { DashboardView } from "@/app/page";

type FindingType =
  | "credential_exposure"
  | "infrastructure_cluster"
  | "cross_bot_pattern";
type FindingStatus =
  | "new"
  | "triaged"
  | "in_progress"
  | "resolved"
  | "dismissed"
  | "suppressed";

type Finding = {
  id: string;
  type: FindingType;
  canonical_key: string;
  title: string;
  summary: string;
  why_it_matters: string;
  recommended_action: string;
  confidence: number;
  severity: "low" | "medium" | "high" | "critical";
  priority: number;
  score_explanation: Record<string, unknown>;
  status: FindingStatus;
  assignee?: string | null;
  first_seen_at: string;
  last_seen_at: string;
  evidence_count: number;
  material_version: number;
  last_material_change_at: string;
};

type FindingEvidence = {
  id: string;
  finding_id: string;
  evidence_type: string;
  source_table: string;
  source_id: string;
  observed_at: string;
  weight: number;
  excerpt_redacted?: string | null;
  provenance?: Record<string, unknown> | null;
};

type FeedbackAction = {
  label: "useful" | "noise" | "actioned";
  reason: "actionable" | "false_positive" | "confirmed";
  status: FindingStatus;
  successText: string;
};

const FINDING_LABELS: Record<FindingType, string> = {
  credential_exposure: "Credential exposure",
  infrastructure_cluster: "Infrastructure cluster",
  cross_bot_pattern: "Cross-bot pattern",
};

const STATUS_LABELS: Record<FindingStatus, string> = {
  new: "New",
  triaged: "Triaged",
  in_progress: "In progress",
  resolved: "Resolved",
  dismissed: "Dismissed",
  suppressed: "Suppressed",
};

const OPEN_STATUSES: FindingStatus[] = ["new", "triaged", "in_progress"];

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown time";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function priorityTone(priority: number) {
  if (priority >= 9) return "bg-red-600 text-white";
  if (priority >= 7) return "bg-orange-500 text-white";
  if (priority >= 4) return "bg-amber-400 text-slate-950";
  return "bg-slate-300 text-slate-800";
}

function severityTone(severity: Finding["severity"]) {
  return {
    critical: "border-red-200 bg-red-50 text-red-800",
    high: "border-orange-200 bg-orange-50 text-orange-800",
    medium: "border-amber-200 bg-amber-50 text-amber-800",
    low: "border-slate-200 bg-slate-50 text-slate-700",
  }[severity];
}

function explanationRows(explanation: Record<string, unknown>) {
  return (["confidence", "severity", "priority"] as const).map((kind) => {
    const section = explanation[kind];
    const record =
      section && typeof section === "object"
        ? (section as Record<string, unknown>)
        : {};
    const contributors = Array.isArray(record.contributors)
      ? record.contributors
          .map((item) =>
            item && typeof item === "object"
              ? String((item as Record<string, unknown>).name || "")
              : "",
          )
          .filter(Boolean)
          .slice(0, 3)
      : [];
    return {
      kind,
      value: String(record.value ?? "—"),
      contributors,
    };
  });
}

async function resolveCredentialId(finding: Finding, rows: FindingEvidence[]) {
  if (finding.canonical_key.startsWith("credential:")) {
    return finding.canonical_key.slice("credential:".length);
  }
  const credentialEvidence = rows.find(
    (item) => item.source_table === "discovered_credentials",
  );
  if (credentialEvidence) return credentialEvidence.source_id;

  const messageIds = rows
    .filter((item) => item.source_table === "exfiltrated_messages")
    .map((item) => item.source_id)
    .slice(0, 50);
  if (!messageIds.length) return null;
  const { data } = await supabase
    .from("exfiltrated_messages")
    .select("credential_id")
    .in("id", messageIds)
    .limit(1);
  return data?.[0]?.credential_id ? String(data[0].credential_id) : null;
}

async function fetchFindingEvidence(finding: Finding) {
  const { data, error } = await supabase
    .from("finding_evidence")
    .select(
      "id,finding_id,evidence_type,source_table,source_id,observed_at," +
        "weight,excerpt_redacted,provenance",
    )
    .eq("finding_id", finding.id)
    .order("observed_at", { ascending: false })
    .limit(200);
  if (error) return { rows: [] as FindingEvidence[], credentialId: null, error };
  const rows = (data || []) as unknown as FindingEvidence[];
  return {
    rows,
    credentialId: await resolveCredentialId(finding, rows),
    error: null,
  };
}

export default function FindingsQueue({
  onDrilldown,
}: {
  onDrilldown: (credentialId: string, view: Extract<DashboardView, "chat" | "botTelemetry">) => void;
}) {
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [typeFilter, setTypeFilter] = useState<"all" | FindingType>("all");
  const [statusFilter, setStatusFilter] = useState<"open" | "all" | FindingStatus>("open");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [evidence, setEvidence] = useState<Record<string, FindingEvidence[]>>({});
  const [credentialByFinding, setCredentialByFinding] = useState<Record<string, string | null>>({});
  const [pendingFinding, setPendingFinding] = useState<string | null>(null);
  const [noticeByFinding, setNoticeByFinding] = useState<Record<string, string>>({});
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    let active = true;

    async function fetchFindings() {
      setLoading(true);
      setError(null);
      let query = supabase
        .from("findings")
        .select(
          "id,type,canonical_key,title,summary,why_it_matters,recommended_action," +
            "confidence,severity,priority,score_explanation,status,assignee," +
            "first_seen_at,last_seen_at,evidence_count,material_version,last_material_change_at",
        );
      if (typeFilter !== "all") query = query.eq("type", typeFilter);
      if (statusFilter === "open") query = query.in("status", OPEN_STATUSES);
      else if (statusFilter !== "all") query = query.eq("status", statusFilter);

      const { data, error: queryError } = await query
        .order("priority", { ascending: false })
        .order("last_material_change_at", { ascending: false })
        .limit(200);
      if (!active) return;
      if (queryError) {
        setFindings([]);
        setError(`Findings could not be loaded: ${queryError.message}`);
      } else {
        const rows = (data || []) as unknown as Finding[];
        setFindings(rows);
        const requested = new URLSearchParams(window.location.search).get("finding");
        const requestedFinding = rows.find((row) => row.id === requested);
        if (requestedFinding) {
          setExpanded((current) => new Set(current).add(requestedFinding.id));
          const details = await fetchFindingEvidence(requestedFinding);
          if (!active) return;
          if (details.error) {
            setNoticeByFinding((current) => ({
              ...current,
              [requestedFinding.id]: `Evidence could not be loaded: ${details.error.message}`,
            }));
          } else {
            setEvidence((current) => ({
              ...current,
              [requestedFinding.id]: details.rows,
            }));
            setCredentialByFinding((current) => ({
              ...current,
              [requestedFinding.id]: details.credentialId,
            }));
          }
        }
      }
      setLoading(false);
    }

    fetchFindings();
    return () => {
      active = false;
    };
  }, [refreshKey, statusFilter, typeFilter]);

  const queueSummary = useMemo(() => {
    const critical = findings.filter((finding) => finding.severity === "critical").length;
    const unassigned = findings.filter((finding) => !finding.assignee).length;
    return `${findings.length} shown · ${critical} critical · ${unassigned} unassigned`;
  }, [findings]);

  async function loadEvidence(finding: Finding) {
    if (evidence[finding.id]) return;
    const details = await fetchFindingEvidence(finding);
    if (details.error) {
      setNoticeByFinding((current) => ({
        ...current,
        [finding.id]: `Evidence could not be loaded: ${details.error.message}`,
      }));
      return;
    }
    setEvidence((current) => ({ ...current, [finding.id]: details.rows }));
    setCredentialByFinding((current) => ({
      ...current,
      [finding.id]: details.credentialId,
    }));
  }

  async function toggleFinding(finding: Finding) {
    const isOpen = expanded.has(finding.id);
    setExpanded((current) => {
      const next = new Set(current);
      if (isOpen) next.delete(finding.id);
      else next.add(finding.id);
      return next;
    });
    if (!isOpen) await loadEvidence(finding);
  }

  async function recordFeedback(finding: Finding, action: FeedbackAction) {
    setPendingFinding(finding.id);
    setNoticeByFinding((current) => ({ ...current, [finding.id]: "" }));
    const { error: feedbackError } = await supabase.rpc("record_finding_feedback", {
      p_finding_id: finding.id,
      p_label: action.label,
      p_reason_code: action.reason,
      p_note: null,
      p_status: action.status,
      p_assignee: null,
      p_suppress_pattern: null,
    });
    setPendingFinding(null);
    if (feedbackError) {
      setNoticeByFinding((current) => ({
        ...current,
        [finding.id]: `Feedback was not saved: ${feedbackError.message}`,
      }));
      return;
    }
    setFindings((current) =>
      current.map((row) =>
        row.id === finding.id ? { ...row, status: action.status } : row,
      ),
    );
    setNoticeByFinding((current) => ({
      ...current,
      [finding.id]: action.successText,
    }));
  }

  return (
    <section className="h-full overflow-y-auto bg-[#f4f7fa]" aria-label="Findings queue">
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-[#f4f7fa]/95 px-4 py-4 backdrop-blur md:px-7">
        <div className="flex flex-wrap items-end justify-between gap-3">
          <div>
            <p className="font-mono text-[11px] uppercase tracking-[0.24em] text-[#197e74]">
              Material deltas
            </p>
            <h1 className="font-[Arial_Narrow,Arial,sans-serif] text-3xl font-black tracking-tight text-[#172033]">
              Findings queue
            </h1>
            <p className="mt-1 text-sm text-slate-600">{queueSummary}</p>
          </div>
          <button
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            className="flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 shadow-sm hover:border-[#197e74] hover:text-[#197e74] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
          >
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        </div>

        <div className="mt-4 flex flex-wrap gap-2" aria-label="Finding filters">
          <select
            aria-label="Finding type"
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value as typeof typeFilter)}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
          >
            <option value="all">All finding types</option>
            {Object.entries(FINDING_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
          <select
            aria-label="Finding status"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
          >
            <option value="open">Open work</option>
            <option value="all">All statuses</option>
            {Object.entries(STATUS_LABELS).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="mx-auto flex max-w-5xl flex-col gap-3 p-4 md:p-7">
        {loading && (
          <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white p-5 text-sm text-slate-600">
            <Loader2 className="h-4 w-4 animate-spin motion-reduce:animate-none" /> Loading material findings…
          </div>
        )}
        {!loading && error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800" role="alert">
            {error} Check that the Insight Queue migration is applied, then refresh.
          </div>
        )}
        {!loading && !error && findings.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
            <CheckCircle2 className="mx-auto h-7 w-7 text-[#197e74]" />
            <h2 className="mt-3 font-bold text-slate-900">No findings match this view</h2>
            <p className="mt-1 text-sm text-slate-600">Change the filters or wait for the next producer run.</p>
          </div>
        )}

        {findings.map((finding) => {
          const isOpen = expanded.has(finding.id);
          const rows = evidence[finding.id] || [];
          const credentialId = credentialByFinding[finding.id];
          const pending = pendingFinding === finding.id;
          return (
            <article
              key={finding.id}
              className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-[0_1px_0_rgba(23,32,51,0.04)]"
            >
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => toggleFinding(finding)}
                className="grid w-full grid-cols-[3rem_1fr_auto] gap-3 p-4 text-left hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-[#197e74] md:grid-cols-[4rem_1fr_auto] md:p-5"
              >
                <div className={`flex h-12 flex-col items-center justify-center rounded-md font-mono ${priorityTone(finding.priority)}`}>
                  <span className="text-[9px] font-bold uppercase tracking-widest">Priority</span>
                  <span className="text-xl font-black leading-none">{finding.priority}</span>
                </div>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`rounded-full border px-2 py-0.5 text-[11px] font-bold uppercase tracking-wide ${severityTone(finding.severity)}`}>
                      {finding.severity}
                    </span>
                    <span className="font-mono text-[11px] text-slate-500">{FINDING_LABELS[finding.type]}</span>
                    <span className="font-mono text-[11px] text-slate-400">v{finding.material_version}</span>
                  </div>
                  <h2 className="mt-1 truncate text-base font-bold text-[#172033] md:text-lg">{finding.title}</h2>
                  <p className="mt-1 line-clamp-2 text-sm leading-5 text-slate-600">{finding.summary}</p>
                  <div className="mt-3 flex items-center gap-1" aria-label={`Priority ${finding.priority} of 10`}>
                    {Array.from({ length: 10 }, (_, index) => (
                      <span
                        key={index}
                        className={`h-1.5 flex-1 rounded-sm ${index < finding.priority ? "bg-[#e5573f]" : "bg-slate-200"}`}
                      />
                    ))}
                  </div>
                </div>
                <div className="flex items-start gap-2 text-slate-500">
                  <span className="hidden whitespace-nowrap font-mono text-[11px] md:inline">{finding.evidence_count} evidence</span>
                  {isOpen ? <ChevronDown className="h-5 w-5" /> : <ChevronRight className="h-5 w-5" />}
                </div>
              </button>

              {isOpen && (
                <div className="border-t border-slate-200 bg-slate-50/70 px-4 py-5 md:px-8">
                  <div className="grid gap-5 lg:grid-cols-[1.2fr_0.8fr]">
                    <div className="space-y-4">
                      <div>
                        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Why it matters</p>
                        <p className="mt-1 text-sm leading-6 text-slate-800">{finding.why_it_matters}</p>
                      </div>
                      <div className="rounded-md border-l-4 border-[#197e74] bg-white p-3">
                        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-[#197e74]">Recommended next action</p>
                        <p className="mt-1 text-sm leading-6 text-slate-800">{finding.recommended_action}</p>
                      </div>
                      <div>
                        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Evidence trail</p>
                        <div className="mt-2 space-y-2">
                          {rows.length === 0 ? (
                            <p className="text-sm text-slate-500">No redacted evidence rows are available.</p>
                          ) : rows.map((item) => (
                            <div key={item.id} className="relative border-l-2 border-slate-300 pl-4 text-sm before:absolute before:-left-[5px] before:top-1.5 before:h-2 before:w-2 before:rounded-full before:bg-[#e5573f]">
                              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                                <span className="font-mono font-semibold text-slate-700">{item.evidence_type}</span>
                                <span>{formatTime(item.observed_at)}</span>
                                <span>{Math.round(item.weight * 100)}% weight</span>
                              </div>
                              {item.excerpt_redacted && <p className="mt-1 text-slate-700">{item.excerpt_redacted}</p>}
                              <p className="mt-1 font-mono text-[10px] text-slate-400">{item.source_table} · {item.source_id.slice(0, 12)}</p>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>

                    <aside className="space-y-4">
                      <div className="rounded-md border border-slate-200 bg-white p-3">
                        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Scoring explanation</p>
                        <dl className="mt-2 space-y-2">
                          {explanationRows(finding.score_explanation).map((row) => (
                            <div key={row.kind} className="grid grid-cols-[5rem_1fr] gap-2 text-xs">
                              <dt className="font-semibold capitalize text-slate-700">{row.kind}</dt>
                              <dd className="text-slate-500">
                                {row.value}{row.contributors.length ? ` · ${row.contributors.join(", ")}` : ""}
                              </dd>
                            </div>
                          ))}
                        </dl>
                      </div>

                      <div>
                        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Record disposition</p>
                        <div className="mt-2 grid grid-cols-2 gap-2">
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "useful", reason: "actionable", status: "triaged", successText: "Marked useful and triaged." })} className="flex items-center justify-center gap-1 rounded-md border border-emerald-200 bg-emerald-50 px-2 py-2 text-xs font-bold text-emerald-800 hover:bg-emerald-100 disabled:opacity-50"><ThumbsUp className="h-3.5 w-3.5" /> Useful</button>
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "noise", reason: "false_positive", status: "dismissed", successText: "Marked noise and dismissed." })} className="flex items-center justify-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50"><ThumbsDown className="h-3.5 w-3.5" /> Noise</button>
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "actioned", reason: "confirmed", status: "in_progress", successText: "Moved to in progress." })} className="rounded-md border border-blue-200 bg-blue-50 px-2 py-2 text-xs font-bold text-blue-800 hover:bg-blue-100 disabled:opacity-50">Start work</button>
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "actioned", reason: "confirmed", status: "resolved", successText: "Marked resolved." })} className="rounded-md border border-[#197e74]/30 bg-[#197e74]/10 px-2 py-2 text-xs font-bold text-[#12645c] hover:bg-[#197e74]/15 disabled:opacity-50">Resolve</button>
                        </div>
                        {pending && <p className="mt-2 flex items-center gap-1 text-xs text-slate-500"><Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" /> Saving feedback…</p>}
                        {noticeByFinding[finding.id] && <p className="mt-2 text-xs text-slate-600" role="status">{noticeByFinding[finding.id]}</p>}
                      </div>

                      <div>
                        <p className="font-mono text-[10px] font-bold uppercase tracking-[0.18em] text-slate-500">Drill down</p>
                        {credentialId ? (
                          <div className="mt-2 flex gap-2">
                            <button onClick={() => onDrilldown(credentialId, "chat")} className="flex flex-1 items-center justify-center gap-1 rounded-md bg-[#172033] px-2 py-2 text-xs font-bold text-white hover:bg-slate-700"><MessageSquareText className="h-3.5 w-3.5" /> Chat</button>
                            <button onClick={() => onDrilldown(credentialId, "botTelemetry")} className="flex flex-1 items-center justify-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-2 text-xs font-bold text-slate-700 hover:border-[#197e74] hover:text-[#197e74]"><Radar className="h-3.5 w-3.5" /> Telemetry</button>
                          </div>
                        ) : (
                          <p className="mt-2 flex gap-2 text-xs text-slate-500"><CircleAlert className="h-4 w-4 shrink-0" /> No credential drill-down is linked to this evidence set.</p>
                        )}
                      </div>
                      <p className="font-mono text-[10px] text-slate-400">Changed {formatTime(finding.last_material_change_at)} · ID {finding.id.slice(0, 8)}</p>
                    </aside>
                  </div>
                </div>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
