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

type EvidenceSourceDetail = {
  content?: string | null;
  credential_id?: string | null;
  sender_name?: string | null;
  sender_user_id?: number | null;
  media_type?: string | null;
  created_at?: string | null;
  chat_id?: number | null;
  chat_name?: string | null;
  chat_type?: string | null;
  source?: string | null;
  status?: string | null;
  bot_username?: string | null;
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
  source_detail?: EvidenceSourceDetail | null;
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
  if (priority >= 7) return "bg-ink text-white";
  if (priority >= 4) return "bg-ink text-white";
  return "bg-slate-200 text-slate-700";
}

function severityTone(severity: Finding["severity"]) {
  return {
    critical: "border-red-200 bg-red-50 text-red-800",
    high: "border-red-200 bg-red-50 text-red-800",
    medium: "border-slate-300 bg-slate-100 text-slate-700",
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
    .from("evidence_redacted")
    .select("credential_id")
    .in("id", messageIds)
    .limit(1);
  return data?.[0]?.credential_id ? String(data[0].credential_id) : null;
}

const EVIDENCE_PAGE_SIZE = 200;
const SOURCE_LOOKUP_CHUNK = 50;
const SESSION_EXPIRED_EVIDENCE =
  "Your operator session expired, so the evidence could not be read. Sign in again to reload it.";

function chunk<T>(items: T[], size: number): T[][] {
  const chunks: T[][] = [];
  for (let index = 0; index < items.length; index += size) {
    chunks.push(items.slice(index, index + size));
  }
  return chunks;
}

async function resolveSourceDetails(rows: FindingEvidence[]) {
  const details = new Map<string, EvidenceSourceDetail>();
  const messageIds = [
    ...new Set(
      rows
        .filter((item) => item.source_table === "exfiltrated_messages")
        .map((item) => item.source_id),
    ),
  ];
  const credentialIds = [
    ...new Set(
      rows
        .filter((item) => item.source_table === "discovered_credentials")
        .map((item) => item.source_id),
    ),
  ];

  let rawLookupFailed = false;
  for (const ids of chunk(messageIds, SOURCE_LOOKUP_CHUNK)) {
    try {
      const { data, error } = await supabase
        .from("evidence_operator_raw")
        .select(
          "id,credential_id,sender_name,sender_user_id,content,media_type," +
            "created_at,chat_id,chat_name,source,status,bot_username",
        )
        .in("id", ids)
        .limit(SOURCE_LOOKUP_CHUNK);
      if (error) {
        rawLookupFailed = true;
        break;
      }
      for (const row of (data || []) as unknown as Array<
        Record<string, unknown>
      >) {
        details.set(`exfiltrated_messages:${String(row.id)}`, {
          content: typeof row.content === "string" ? row.content : null,
          credential_id:
            typeof row.credential_id === "string" ? row.credential_id : null,
          sender_name:
            typeof row.sender_name === "string" ? row.sender_name : null,
          sender_user_id:
            typeof row.sender_user_id === "number" ? row.sender_user_id : null,
          media_type: typeof row.media_type === "string" ? row.media_type : null,
          created_at:
            typeof row.created_at === "string" ? row.created_at : null,
          chat_id: typeof row.chat_id === "number" ? row.chat_id : null,
          chat_name: typeof row.chat_name === "string" ? row.chat_name : null,
          source: typeof row.source === "string" ? row.source : null,
          status: typeof row.status === "string" ? row.status : null,
          bot_username:
            typeof row.bot_username === "string" ? row.bot_username : null,
        });
      }
    } catch {
      rawLookupFailed = true;
      break;
    }
  }

  if (rawLookupFailed) {
    for (const ids of chunk(messageIds, SOURCE_LOOKUP_CHUNK)) {
      const { data, error } = await supabase
        .from("evidence_redacted")
        .select("id,credential_id,content,media_type,created_at")
        .in("id", ids)
        .limit(SOURCE_LOOKUP_CHUNK);
      if (error) continue;
      for (const row of (data || []) as Array<Record<string, unknown>>) {
        details.set(`exfiltrated_messages:${String(row.id)}`, {
          content: typeof row.content === "string" ? row.content : null,
          credential_id:
            typeof row.credential_id === "string" ? row.credential_id : null,
          media_type:
            typeof row.media_type === "string" ? row.media_type : null,
          created_at:
            typeof row.created_at === "string" ? row.created_at : null,
        });
      }
    }
  }

  for (const ids of chunk(credentialIds, SOURCE_LOOKUP_CHUNK)) {
    const { data, error } = await supabase
      .from("discovered_credentials_public")
      .select("id,source,status,chat_id,chat_name,chat_type,meta")
      .in("id", ids)
      .limit(SOURCE_LOOKUP_CHUNK);
    if (error) continue;
    for (const row of (data || []) as Array<Record<string, unknown>>) {
      const meta = (row.meta || {}) as Record<string, unknown>;
      details.set(`discovered_credentials:${String(row.id)}`, {
        credential_id: String(row.id),
        chat_id: typeof row.chat_id === "number" ? row.chat_id : null,
        chat_name: typeof row.chat_name === "string" ? row.chat_name : null,
        chat_type: typeof row.chat_type === "string" ? row.chat_type : null,
        source: typeof row.source === "string" ? row.source : null,
        status: typeof row.status === "string" ? row.status : null,
        bot_username:
          typeof meta.bot_username === "string" ? meta.bot_username : null,
      });
    }
  }

  return details;
}

async function fetchFindingEvidence(finding: Finding, offset = 0) {
  const { data: sessionData, error: sessionError } = await supabase.auth.getSession();
  if (sessionError || !sessionData.session) {
    return {
      rows: [] as FindingEvidence[],
      credentialId: null,
      error: null,
      sessionExpired: true,
    };
  }

  const { data, error } = await supabase
    .from("finding_evidence")
    .select(
      "id,finding_id,evidence_type,source_table,source_id,observed_at," +
        "weight,excerpt_redacted,provenance",
    )
    .eq("finding_id", finding.id)
    .order("observed_at", { ascending: false })
    .range(offset, offset + EVIDENCE_PAGE_SIZE - 1);
  if (error) {
    const code = (error as { code?: string }).code;
    const sessionExpired =
      code === "42501" || /permission denied|jwt|session/i.test(error.message);
    return {
      rows: [] as FindingEvidence[],
      credentialId: null,
      error,
      sessionExpired,
    };
  }

  const baseRows = (data || []) as unknown as FindingEvidence[];
  const sourceDetails = await resolveSourceDetails(baseRows);
  const rows = baseRows.map((item) => ({
    ...item,
    source_detail:
      sourceDetails.get(`${item.source_table}:${item.source_id}`) ?? null,
  }));
  return {
    rows,
    credentialId: await resolveCredentialId(finding, rows),
    error: null,
    sessionExpired: false,
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
  const [evidenceLoading, setEvidenceLoading] = useState<Record<string, boolean>>({});
  const [evidenceError, setEvidenceError] = useState<Record<string, string>>({});
  const [evidenceSessionExpired, setEvidenceSessionExpired] = useState<Record<string, boolean>>({});
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
        )
        .gt("evidence_count", 0);
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
        const rows = ((data || []) as unknown as Finding[]).filter(
          (row) => row.evidence_count > 0,
        );
        setFindings(rows);
        const requested = new URLSearchParams(window.location.search).get("finding");
        const requestedFinding = rows.find((row) => row.id === requested);
        if (requestedFinding) {
          setExpanded((current) => new Set(current).add(requestedFinding.id));
          setEvidenceLoading((current) => ({
            ...current,
            [requestedFinding.id]: true,
          }));
          const details = await fetchFindingEvidence(requestedFinding);
          if (!active) return;
          setEvidenceLoading((current) => ({
            ...current,
            [requestedFinding.id]: false,
          }));
          if (details.sessionExpired) {
            setEvidenceSessionExpired((current) => ({
              ...current,
              [requestedFinding.id]: true,
            }));
            setEvidenceError((current) => ({
              ...current,
              [requestedFinding.id]: SESSION_EXPIRED_EVIDENCE,
            }));
          } else if (details.error) {
            setEvidenceError((current) => ({
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

  async function loadEvidence(finding: Finding, force = false) {
    if (
      !force &&
      (evidence[finding.id] !== undefined || evidenceLoading[finding.id])
    ) {
      return;
    }
    setEvidenceLoading((current) => ({ ...current, [finding.id]: true }));
    setEvidenceError((current) => ({ ...current, [finding.id]: "" }));
    setEvidenceSessionExpired((current) => ({
      ...current,
      [finding.id]: false,
    }));
    const details = await fetchFindingEvidence(finding);
    setEvidenceLoading((current) => ({ ...current, [finding.id]: false }));
    if (details.sessionExpired) {
      setEvidenceSessionExpired((current) => ({
        ...current,
        [finding.id]: true,
      }));
      setEvidenceError((current) => ({
        ...current,
        [finding.id]: SESSION_EXPIRED_EVIDENCE,
      }));
      return;
    }
    if (details.error) {
      setEvidenceError((current) => ({
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

  async function loadMoreEvidence(finding: Finding) {
    const currentRows = evidence[finding.id] || [];
    if (
      evidenceLoading[finding.id] ||
      currentRows.length >= finding.evidence_count
    ) {
      return;
    }
    setEvidenceLoading((current) => ({ ...current, [finding.id]: true }));
    setEvidenceError((current) => ({ ...current, [finding.id]: "" }));
    setEvidenceSessionExpired((current) => ({
      ...current,
      [finding.id]: false,
    }));
    const details = await fetchFindingEvidence(finding, currentRows.length);
    setEvidenceLoading((current) => ({ ...current, [finding.id]: false }));
    if (details.sessionExpired) {
      setEvidenceSessionExpired((current) => ({
        ...current,
        [finding.id]: true,
      }));
      setEvidenceError((current) => ({
        ...current,
        [finding.id]: SESSION_EXPIRED_EVIDENCE,
      }));
      return;
    }
    if (details.error) {
      setEvidenceError((current) => ({
        ...current,
        [finding.id]: `Evidence could not be loaded: ${details.error.message}`,
      }));
      return;
    }
    setEvidence((current) => ({
      ...current,
      [finding.id]: [...(current[finding.id] || []), ...details.rows],
    }));
    setCredentialByFinding((current) => ({
      ...current,
      [finding.id]: details.credentialId,
    }));
  }

  async function retryEvidence(finding: Finding) {
    setEvidence((current) => {
      const next = { ...current };
      delete next[finding.id];
      return next;
    });
    await loadEvidence(finding, true);
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
    <section className="h-full overflow-y-auto bg-paper" aria-label="Findings queue">
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-paper/95 px-4 py-4 backdrop-blur md:px-7">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="sr-only">Findings queue</h1>
          <p className="text-sm text-slate-600">{queueSummary}</p>
          <button
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            className="flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 shadow-sm hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <RefreshCw className="h-4 w-4" /> Refresh
          </button>
        </div>

        <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label="Finding filters">
          <select
            aria-label="Finding type"
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value as typeof typeFilter)}
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
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
            className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
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
          <div
            data-testid="findings-skeleton"
            aria-busy="true"
            aria-label="Loading findings"
            className="space-y-3"
          >
            <span className="sr-only">Loading material findings…</span>
            {Array.from({ length: 3 }, (_, index) => (
              <div
                key={index}
                className="animate-pulse rounded-lg border border-slate-200 bg-white p-5 motion-reduce:animate-none"
              >
                <div className="flex items-center gap-3">
                  <div className="h-12 w-12 rounded-md bg-slate-200" />
                  <div className="flex-1 space-y-2">
                    <div className="h-2.5 w-24 rounded bg-slate-200" />
                    <div className="h-3.5 w-2/3 rounded bg-slate-200" />
                  </div>
                </div>
                <div className="mt-3 space-y-2">
                  <div className="h-2.5 w-full rounded bg-slate-100" />
                  <div className="h-2.5 w-4/5 rounded bg-slate-100" />
                </div>
              </div>
            ))}
          </div>
        )}
        {!loading && error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800" role="alert">
            {error} Check that the Insight Queue migration is applied, then refresh.
          </div>
        )}
        {!loading && !error && findings.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
            <CheckCircle2 className="mx-auto h-7 w-7 text-accent" />
            <h2 className="mt-3 font-bold text-slate-900">No findings match this view</h2>
            <p className="mt-1 text-sm text-slate-600">Change the filters or wait for the next producer run.</p>
            {(typeFilter !== "all" || statusFilter !== "all") && (
              <button
                type="button"
                onClick={() => {
                  setTypeFilter("all");
                  setStatusFilter("all");
                }}
                className="mt-3 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              >
                Show all findings
              </button>
            )}
          </div>
        )}

        <div role="list" aria-label="Findings" aria-busy={loading} className="flex flex-col gap-3">
        {findings.map((finding) => {
          const isOpen = expanded.has(finding.id);
          const rows = evidence[finding.id] || [];
          const credentialId = credentialByFinding[finding.id];
          const evidenceBusy = evidenceLoading[finding.id] ?? false;
          const evidenceFailure = evidenceError[finding.id];
          const evidenceExpired = evidenceSessionExpired[finding.id] ?? false;
          const pending = pendingFinding === finding.id;
          return (
            <div role="listitem" key={finding.id} className="contents">
            <article
              className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-[0_1px_0_rgba(23,32,51,0.04)]"
            >
              <button
                type="button"
                aria-expanded={isOpen}
                onClick={() => toggleFinding(finding)}
                className="grid w-full grid-cols-[3.5rem_1fr_auto] gap-3 p-4 text-left hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-accent md:grid-cols-[4.5rem_1fr_auto] md:p-5"
              >
                <div className={`flex h-12 flex-col items-center justify-center rounded-md font-mono ${priorityTone(finding.priority)}`}>
                  <span className="text-xs font-bold uppercase">Priority</span>
                  <span className="text-lg font-bold leading-none">{finding.priority}</span>
                </div>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`rounded-full border px-2 py-0.5 text-xs font-bold uppercase tracking-wide ${severityTone(finding.severity)}`}>
                      {finding.severity}
                    </span>
                    <span className="font-mono text-xs text-slate-500">{FINDING_LABELS[finding.type]}</span>
                    <span className="font-mono text-xs text-slate-400">v{finding.material_version}</span>
                  </div>
                  <h2 className="mt-1 truncate text-sm font-bold text-ink md:text-lg">{finding.title}</h2>
                  <p className="mt-1 line-clamp-2 text-sm leading-5 text-slate-600">{finding.summary}</p>
                  <div className="mt-3 flex items-center gap-1" aria-label={`Priority ${finding.priority} of 10`}>
                    {Array.from({ length: 10 }, (_, index) => (
                      <span
                        key={index}
                        className={`h-1.5 flex-1 rounded-sm ${index < finding.priority ? "bg-accent" : "bg-slate-200"}`}
                      />
                    ))}
                  </div>
                </div>
                <div className="flex items-start gap-2 text-slate-500">
                  <span className="hidden whitespace-nowrap font-mono text-xs md:inline">{finding.evidence_count} evidence</span>
                  {isOpen ? <ChevronDown className="h-5 w-5" /> : <ChevronRight className="h-5 w-5" />}
                </div>
              </button>

              {isOpen && (
                <div className="border-t border-slate-200 bg-slate-50/70 px-4 py-5 md:px-8">
                  <div className="grid gap-5 lg:grid-cols-[1.2fr_0.8fr]">
                    <div className="space-y-4">
                      <div>
                        <p className="font-mono text-xs font-bold uppercase tracking-[0.18em] text-slate-500">Why it matters</p>
                        <p className="mt-1 text-sm leading-6 text-slate-800">{finding.why_it_matters}</p>
                      </div>
                      <div className="rounded-md border-l-4 border-accent bg-white p-3">
                        <p className="font-mono text-xs font-bold uppercase tracking-[0.18em] text-accent">Recommended next action</p>
                        <p className="mt-1 text-sm leading-6 text-slate-800">{finding.recommended_action}</p>
                      </div>
                      <div>
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <p className="font-mono text-xs font-bold uppercase tracking-[0.18em] text-slate-500">Evidence trail</p>
                          {finding.evidence_count > 0 && (
                            <span className="font-mono text-xs text-slate-500">
                              Showing {rows.length} of {finding.evidence_count}
                            </span>
                          )}
                        </div>
                        <div className="mt-2 space-y-2">
                          {evidenceFailure && (
                            <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
                              <p>{evidenceFailure}</p>
                              <div className="mt-2 flex flex-wrap items-center gap-3">
                                <button
                                  type="button"
                                  onClick={() => { void retryEvidence(finding); }}
                                  className="rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                                >
                                  Retry
                                </button>
                                {evidenceExpired && (
                                  <a href="/signin" className="text-xs font-bold underline">Sign in again</a>
                                )}
                              </div>
                            </div>
                          )}
                          {evidenceBusy && rows.length === 0 && (
                            <p className="text-sm text-slate-500">Loading evidence…</p>
                          )}
                          {!evidenceBusy && !evidenceFailure && rows.length === 0 && (
                            finding.evidence_count > 0 ? (
                              <div className="rounded-md border border-slate-300 bg-slate-100 p-3 text-sm text-slate-700">
                                <p>The index reports {finding.evidence_count} evidence records, but none were returned.</p>
                                <button
                                  type="button"
                                  onClick={() => { void retryEvidence(finding); }}
                                  className="mt-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                                >
                                  Retry
                                </button>
                              </div>
                            ) : (
                              <p className="text-sm text-slate-500">No redacted evidence rows are available.</p>
                            )
                          )}
                          {rows.map((item) => {
                            const detail = item.source_detail;
                            const detailCredentialId = detail?.credential_id ?? null;
                            return (
                              <div key={item.id} className="relative border-l-2 border-slate-300 pl-4 text-sm before:absolute before:-left-[5px] before:top-1.5 before:h-2 before:w-2 before:rounded-full before:bg-accent">
                                <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
                                  <span className="font-mono font-semibold text-slate-700">{item.evidence_type}</span>
                                  <span>{formatTime(item.observed_at)}</span>
                                  <span>{Math.round(item.weight * 100)}% weight</span>
                                </div>
                                {detail?.content ? (
                                  <p className="mt-1 whitespace-pre-wrap text-slate-700">{detail.content}</p>
                                ) : item.excerpt_redacted ? (
                                  <p className="mt-1 text-slate-700">{item.excerpt_redacted}</p>
                                ) : null}
                                {item.source_table === "exfiltrated_messages" && !detail && (
                                  <p className="mt-1 text-xs text-slate-500">
                                    Source message was pruned after broadcast; only the pseudonymized evidence link remains.
                                  </p>
                                )}
                                {detail && item.source_table === "exfiltrated_messages" && !detail.content && (
                                  <p className="mt-1 text-xs text-slate-500">
                                    Source message contains no text (media or service message).
                                  </p>
                                )}
                                {detail?.sender_name && (
                                  <p className="mt-1 text-xs text-slate-500">
                                    Sender: {detail.sender_name}
                                    {detail.sender_user_id
                                      ? ` · ${detail.sender_user_id}`
                                      : ""}
                                  </p>
                                )}
                                {detail?.chat_name && (
                                  <p className="mt-1 text-xs text-slate-500">
                                    Chat: {detail.chat_name}
                                    {detail.chat_id ? ` · ${detail.chat_id}` : ""}
                                  </p>
                                )}
                                {detailCredentialId && (
                                  <p className="mt-1 font-mono text-xs text-slate-500">
                                    Credential: {detailCredentialId}
                                  </p>
                                )}
                                <p className="mt-1 font-mono text-xs text-slate-400">{item.source_table} · {item.source_id.slice(0, 12)}</p>
                                {detailCredentialId && (
                                  <button
                                    type="button"
                                    onClick={() => onDrilldown(detailCredentialId, "chat")}
                                    className="mt-1 text-xs font-bold text-accent underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                                  >
                                    Open chat
                                  </button>
                                )}
                              </div>
                            );
                          })}
                          {rows.length > 0 && rows.length < finding.evidence_count && (
                            <button
                              type="button"
                              onClick={() => { void loadMoreEvidence(finding); }}
                              disabled={evidenceBusy}
                              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:opacity-50"
                            >
                              {evidenceBusy ? "Loading more…" : `Load more (${rows.length} of ${finding.evidence_count})`}
                            </button>
                          )}
                        </div>
                      </div>
                    </div>

                    <aside className="space-y-4">
                      <div className="rounded-md border border-slate-200 bg-white p-3">
                        <p className="font-mono text-xs font-bold uppercase tracking-[0.18em] text-slate-500">Scoring explanation</p>
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
                        <p className="font-mono text-xs font-bold uppercase tracking-[0.18em] text-slate-500">Record disposition</p>
                        <div className="mt-2 grid grid-cols-2 gap-2">
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "useful", reason: "actionable", status: "triaged", successText: "Marked useful and triaged." })} className="flex items-center justify-center gap-1 rounded-md border border-accent/30 bg-accent/10 px-2 py-2 text-xs font-bold text-accent-strong hover:bg-accent/15 disabled:opacity-50"><ThumbsUp className="h-3.5 w-3.5" /> Useful</button>
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "noise", reason: "false_positive", status: "dismissed", successText: "Marked noise and dismissed." })} className="flex items-center justify-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50"><ThumbsDown className="h-3.5 w-3.5" /> Noise</button>
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "actioned", reason: "confirmed", status: "in_progress", successText: "Moved to in progress." })} className="rounded-md border border-slate-300 bg-white px-2 py-2 text-xs font-bold text-slate-700 hover:bg-slate-100 disabled:opacity-50">Start work</button>
                          <button disabled={pending} onClick={() => recordFeedback(finding, { label: "actioned", reason: "confirmed", status: "resolved", successText: "Marked resolved." })} className="rounded-md border border-accent/30 bg-accent/10 px-2 py-2 text-xs font-bold text-accent-strong hover:bg-accent/15 disabled:opacity-50">Resolve</button>
                        </div>
                        {pending && <p className="mt-2 flex items-center gap-1 text-xs text-slate-500"><Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" /> Saving feedback…</p>}
                        {noticeByFinding[finding.id] && <p className="mt-2 text-xs text-slate-600" role="status">{noticeByFinding[finding.id]}</p>}
                      </div>

                      <div>
                        <p className="font-mono text-xs font-bold uppercase tracking-[0.18em] text-slate-500">Drill down</p>
                        {credentialId ? (
                          <div className="mt-2 flex gap-2">
                            <button onClick={() => onDrilldown(credentialId, "chat")} className="flex flex-1 items-center justify-center gap-1 rounded-md bg-ink px-2 py-2 text-xs font-bold text-white hover:bg-slate-700"><MessageSquareText className="h-3.5 w-3.5" /> Chat</button>
                            <button onClick={() => onDrilldown(credentialId, "botTelemetry")} className="flex flex-1 items-center justify-center gap-1 rounded-md border border-slate-300 bg-white px-2 py-2 text-xs font-bold text-slate-700 hover:border-accent hover:text-accent"><Radar className="h-3.5 w-3.5" /> Telemetry</button>
                          </div>
                        ) : (
                          <p className="mt-2 flex gap-2 text-xs text-slate-500"><CircleAlert className="h-4 w-4 shrink-0" /> No credential drill-down is linked to this evidence set.</p>
                        )}
                      </div>
                      <p className="font-mono text-xs text-slate-400">Changed {formatTime(finding.last_material_change_at)} · ID {finding.id.slice(0, 8)}</p>
                    </aside>
                  </div>
                </div>
              )}
            </article>
            </div>
          );
        })}
        </div>
      </div>
    </section>
  );
}
