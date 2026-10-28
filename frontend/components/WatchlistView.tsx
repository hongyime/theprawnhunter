"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";

import { supabase } from "@/lib/supabase";

export type WatchlistRule = {
  id: string;
  name: string;
  pattern: string;
  is_regex: boolean;
  credential_id: string | null;
  enabled: boolean;
  cooldown_minutes: number;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export const MAX_PATTERN_LENGTH = 200;
export const MIN_COOLDOWN_MINUTES = 1;

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type FormState = {
  name: string;
  pattern: string;
  isRegex: boolean;
  credentialId: string;
  enabled: boolean;
  cooldown: string;
};

const EMPTY_FORM: FormState = {
  name: "",
  pattern: "",
  isRegex: false,
  credentialId: "",
  enabled: true,
  cooldown: "60",
};

function ruleToForm(rule: WatchlistRule): FormState {
  return {
    name: rule.name,
    pattern: rule.pattern,
    isRegex: rule.is_regex,
    credentialId: rule.credential_id ?? "",
    enabled: rule.enabled,
    cooldown: String(rule.cooldown_minutes),
  };
}

export default function WatchlistView() {
  const [rules, setRules] = useState<WatchlistRule[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<WatchlistRule | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [pendingId, setPendingId] = useState<string | null>(null);

  const nameInputRef = useRef<HTMLInputElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const submittingRef = useRef(false);
  const optimisticSeq = useRef(0);

  useEffect(() => {
    submittingRef.current = submitting;
  }, [submitting]);

  useEffect(() => {
    let active = true;

    async function loadRules() {
      setLoading(true);
      setLoadError(null);
      const { data, error } = await supabase.rpc("list_watchlist_rules");
      if (!active) return;
      if (error) {
        setRules([]);
        setLoadError(`Watchlist rules could not be loaded: ${error.message}`);
      } else {
        setRules((data as WatchlistRule[] | null) ?? []);
      }
      setLoading(false);
    }

    loadRules();
    return () => {
      active = false;
    };
  }, [retryToken]);

  // Move focus into the dialog on open and restore it on close.
  useEffect(() => {
    if (!formOpen) return;
    const previouslyFocused =
      openerRef.current ??
      (document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null);
    const timer = window.setTimeout(() => nameInputRef.current?.focus(), 0);

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !submittingRef.current) {
        setFormOpen(false);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", handleKeyDown);
      previouslyFocused?.focus?.();
    };
  }, [formOpen]);

  const patternTooLong = form.pattern.length > MAX_PATTERN_LENGTH;
  const cooldownText = form.cooldown.trim();
  const cooldownInvalid =
    !/^\d+$/.test(cooldownText) ||
    Number(cooldownText) < MIN_COOLDOWN_MINUTES;
  const credentialScope = form.credentialId.trim();
  const scopeInvalid =
    credentialScope.length > 0 && !UUID_PATTERN.test(credentialScope);

  const validationMessages: string[] = [];
  if (patternTooLong) {
    validationMessages.push(
      `Pattern must be ${MAX_PATTERN_LENGTH} characters or fewer.`,
    );
  }
  if (cooldownInvalid) {
    validationMessages.push(
      `Cooldown must be at least ${MIN_COOLDOWN_MINUTES} minute.`,
    );
  }
  if (scopeInvalid) {
    validationMessages.push(
      "Credential scope must be a valid UUID, or blank for a global rule.",
    );
  }

  const canSubmit = !submitting && validationMessages.length === 0;

  function openCreate() {
    openerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setEditing(null);
    setForm(EMPTY_FORM);
    setFormError(null);
    setFormOpen(true);
  }

  function openEdit(rule: WatchlistRule) {
    openerRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    setEditing(rule);
    setForm(ruleToForm(rule));
    setFormError(null);
    setFormOpen(true);
  }

  function closeForm() {
    if (submitting) return;
    setFormOpen(false);
  }

  function updateForm<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    const name = form.name.trim();
    const missing: string[] = [];
    if (!name) missing.push("Name is required.");
    if (!form.pattern) missing.push("Pattern is required.");
    if (missing.length > 0) {
      setFormError(missing.join(" "));
      return;
    }
    if (validationMessages.length > 0) {
      setFormError(validationMessages.join(" "));
      return;
    }

    const previousRules = rules;
    const optimisticId = `optimistic-${Date.now()}-${optimisticSeq.current++}`;
    const optimisticRule: WatchlistRule = {
      id: editing?.id ?? optimisticId,
      name,
      pattern: form.pattern,
      is_regex: form.isRegex,
      credential_id: credentialScope || null,
      enabled: form.enabled,
      cooldown_minutes: Number(cooldownText),
      created_at: new Date().toISOString(),
      updated_at: null,
      created_by: null,
    };

    setFormError(null);
    setActionError(null);
    setSubmitting(true);

    if (editing) {
      setRules((current) =>
        current.map((rule) =>
          rule.id === editing.id ? optimisticRule : rule,
        ),
      );
    } else {
      setRules((current) => [optimisticRule, ...current]);
    }

    const { data, error } = await supabase.rpc("upsert_watchlist_rule", {
      p_id: editing?.id ?? null,
      p_name: name,
      p_pattern: form.pattern,
      p_is_regex: form.isRegex,
      p_credential_id: credentialScope || null,
      p_enabled: form.enabled,
      p_cooldown_minutes: Number(cooldownText),
    });

    if (error) {
      setRules(previousRules);
      setSubmitting(false);
      setFormError(`Rule could not be saved: ${error.message}`);
      return;
    }

    const saved = (data as WatchlistRule | null) ?? null;
    if (editing) {
      setRules((current) =>
        current.map((rule) =>
          rule.id === editing.id ? (saved ?? optimisticRule) : rule,
        ),
      );
    } else {
      setRules((current) =>
        current.map((rule) =>
          rule.id === optimisticId ? (saved ?? optimisticRule) : rule,
        ),
      );
    }
    setSubmitting(false);
    setFormOpen(false);
  }

  async function toggleEnabled(rule: WatchlistRule) {
    const nextEnabled = !rule.enabled;
    setActionError(null);
    setPendingId(rule.id);
    setRules((current) =>
      current.map((row) =>
        row.id === rule.id ? { ...row, enabled: nextEnabled } : row,
      ),
    );

    const { error } = await supabase.rpc("upsert_watchlist_rule", {
      p_id: rule.id,
      p_name: rule.name,
      p_pattern: rule.pattern,
      p_is_regex: rule.is_regex,
      p_credential_id: rule.credential_id,
      p_enabled: nextEnabled,
      p_cooldown_minutes: rule.cooldown_minutes,
    });

    setPendingId(null);
    if (error) {
      setRules((current) =>
        current.map((row) =>
          row.id === rule.id ? { ...row, enabled: rule.enabled } : row,
        ),
      );
      setActionError(
        `Rule “${rule.name}” could not be updated: ${error.message}`,
      );
    }
  }

  async function deleteRule(rule: WatchlistRule) {
    const removedAt = rules.findIndex((row) => row.id === rule.id);
    setActionError(null);
    setConfirmingId(null);
    setPendingId(rule.id);
    setRules((current) => current.filter((row) => row.id !== rule.id));

    const { error } = await supabase.rpc("delete_watchlist_rule", {
      p_id: rule.id,
    });

    setPendingId(null);
    if (error) {
      setRules((current) => {
        const next = [...current];
        const insertAt =
          removedAt < 0 ? next.length : Math.min(removedAt, next.length);
        next.splice(insertAt, 0, rule);
        return next;
      });
      setActionError(
        `Rule “${rule.name}” could not be deleted: ${error.message}`,
      );
    }
  }

  return (
    <section
      className="h-full overflow-y-auto bg-paper"
      aria-label="Watchlist management"
    >
      <div className="sticky top-0 z-10 border-b border-slate-200 bg-paper/95 px-4 py-4 backdrop-blur md:px-7">
        <div className="flex flex-wrap items-center justify-end gap-3">
          <h1 className="sr-only">Watchlists</h1>
          <button
            type="button"
            onClick={openCreate}
            aria-haspopup="dialog"
            className="flex items-center gap-2 rounded-md bg-ink px-3 py-2 text-sm font-bold text-white shadow-sm hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          >
            <Plus className="h-4 w-4" /> New rule
          </button>
        </div>
      </div>

      <div className="mx-auto flex max-w-5xl flex-col gap-3 p-4 md:p-7">
        {actionError && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{actionError}</span>
            <button
              type="button"
              onClick={() => setActionError(null)}
              aria-label="Dismiss error"
              className="rounded p-1 text-red-700 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        {loading && (
          <div className="space-y-2" aria-hidden="true">
            {[0, 1, 2].map((index) => (
              <div
                key={index}
                className="grid grid-cols-[1.2fr_1.6fr_0.8fr_0.7fr_0.8fr] gap-4 rounded-lg border border-slate-200 bg-white p-4 motion-safe:animate-pulse"
              >
                <div className="h-3 w-24 rounded bg-slate-200" />
                <div className="h-3 w-full rounded bg-slate-200" />
                <div className="h-3 w-14 rounded bg-slate-200" />
                <div className="h-3 w-16 rounded bg-slate-200" />
                <div className="h-3 w-12 rounded bg-slate-200" />
              </div>
            ))}
          </div>
        )}

        {!loading && loadError && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-3 rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800"
          >
            <TriangleAlert aria-hidden="true" className="h-4 w-4 shrink-0" />
            <span className="min-w-0 flex-1">{loadError}</span>
            <button
              type="button"
              onClick={() => setRetryToken((value) => value + 1)}
              className="flex items-center gap-1.5 rounded-md border border-red-300 bg-white px-3 py-1.5 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            >
              <RefreshCw className="h-3.5 w-3.5" /> Retry
            </button>
          </div>
        )}

        {!loading && !loadError && rules.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-300 bg-white p-8 text-center">
            <TriangleAlert
              aria-hidden="true"
              className="mx-auto h-7 w-7 text-accent"
            />
            <h2 className="mt-3 font-bold text-slate-900">
              No watchlist rules yet
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              Create a rule to flag keywords or patterns in new messages.
            </p>
          </div>
        )}

        {!loading && !loadError && rules.length > 0 && (
          <div className="overflow-hidden rounded-lg border border-slate-200 bg-white shadow-[0_1px_0_rgba(23,32,51,0.04)]">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[760px] text-left text-sm">
                <thead className="bg-slate-50 font-mono text-xs uppercase tracking-[0.16em] text-slate-500">
                  <tr>
                    <th scope="col" className="px-4 py-3">
                      Rule
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Pattern
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Scope
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Cooldown
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Enabled
                    </th>
                    <th scope="col" className="px-4 py-3 text-right">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {rules.map((rule) => {
                    const pending = pendingId === rule.id;
                    return (
                      <tr key={rule.id} className="align-top">
                        <td className="px-4 py-3">
                          <span className="font-semibold text-slate-800">
                            {rule.name}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex max-w-[24rem] flex-col gap-1">
                            <code
                              className="truncate font-mono text-xs text-slate-700"
                              title={rule.pattern}
                            >
                              {rule.pattern}
                            </code>
                            <span
                              className={`w-fit rounded px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-wide ${
                                rule.is_regex
                                  ? "bg-slate-200 text-slate-700"
                                  : "bg-slate-100 text-slate-600"
                              }`}
                            >
                              {rule.is_regex ? "REGEX" : "TEXT"}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {rule.credential_id ? (
                            <span
                              title={rule.credential_id}
                              className="font-mono text-xs text-slate-600"
                            >
                              {rule.credential_id.slice(0, 8)}…
                            </span>
                          ) : (
                            <span className="rounded bg-accent/10 px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-wide text-accent-strong">
                              Global
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          <span className="font-mono text-xs text-slate-600">
                            {rule.cooldown_minutes} min
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              role="switch"
                              aria-checked={rule.enabled}
                              aria-label={`${rule.enabled ? "Disable" : "Enable"} ${rule.name}`}
                              disabled={pending}
                              onClick={() => toggleEnabled(rule)}
                              className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50 ${
                                rule.enabled ? "bg-accent" : "bg-slate-300"
                              }`}
                            >
                              <span
                                aria-hidden="true"
                                className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                                  rule.enabled
                                    ? "translate-x-4"
                                    : "translate-x-0.5"
                                }`}
                              />
                            </button>
                            <span className="font-mono text-xs text-slate-500">
                              {rule.enabled ? "On" : "Off"}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {confirmingId === rule.id ? (
                            <div className="flex justify-end gap-1">
                              <button
                                type="button"
                                onClick={() => deleteRule(rule)}
                                aria-label={`Confirm delete ${rule.name}`}
                                className="rounded-md border border-red-300 bg-red-50 px-2 py-1 text-xs font-bold text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                              >
                                Confirm
                              </button>
                              <button
                                type="button"
                                onClick={() => setConfirmingId(null)}
                                aria-label={`Cancel delete ${rule.name}`}
                                className="rounded-md border border-slate-300 bg-white px-2 py-1 text-xs font-bold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <div className="flex justify-end gap-1">
                              <button
                                type="button"
                                onClick={() => openEdit(rule)}
                                disabled={pending}
                                aria-label={`Edit ${rule.name}`}
                                aria-haspopup="dialog"
                                className="rounded-md border border-slate-300 bg-white p-1.5 text-slate-600 hover:border-accent hover:text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                <Pencil className="h-3.5 w-3.5" />
                              </button>
                              <button
                                type="button"
                                onClick={() => setConfirmingId(rule.id)}
                                disabled={pending}
                                aria-label={`Delete rule ${rule.name}`}
                                className="rounded-md border border-slate-300 bg-white p-1.5 text-slate-600 hover:border-red-300 hover:text-red-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {formOpen && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-ink/40 p-4"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeForm();
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="watchlist-dialog-title"
            className="max-h-[90vh] w-full max-w-lg overflow-y-auto rounded-lg border border-slate-200 bg-white shadow-xl"
          >
            <div className="flex items-start justify-between border-b border-slate-200 px-5 py-4">
              <div>
                <p className="font-mono text-xs uppercase tracking-[0.2em] text-accent">
                  {editing ? "Edit rule" : "New rule"}
                </p>
                <h2
                  id="watchlist-dialog-title"
                  className="text-lg font-bold text-ink"
                >
                  {editing ? "Edit watchlist rule" : "Create watchlist rule"}
                </h2>
              </div>
              <button
                type="button"
                onClick={closeForm}
                aria-label="Close dialog"
                disabled={submitting}
                className="rounded p-1 text-slate-500 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <form onSubmit={handleSubmit} noValidate>
              <div className="space-y-4 px-5 py-5">
                <div>
                  <label
                    htmlFor="watchlist-name"
                    className="block text-sm font-semibold text-slate-700"
                  >
                    Name
                  </label>
                  <input
                    ref={nameInputRef}
                    id="watchlist-name"
                    type="text"
                    value={form.name}
                    onChange={(event) => updateForm("name", event.target.value)}
                    placeholder="Wallet address mentions"
                    className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent/30"
                  />
                </div>

                <div>
                  <div className="flex items-center justify-between">
                    <label
                      htmlFor="watchlist-pattern"
                      className="block text-sm font-semibold text-slate-700"
                    >
                      Pattern
                    </label>
                    <span
                      className={`font-mono text-xs ${
                        patternTooLong ? "text-red-700" : "text-slate-500"
                      }`}
                    >
                      {form.pattern.length}/{MAX_PATTERN_LENGTH}
                    </span>
                  </div>
                  <textarea
                    id="watchlist-pattern"
                    rows={3}
                    value={form.pattern}
                    onChange={(event) =>
                      updateForm("pattern", event.target.value)
                    }
                    aria-invalid={patternTooLong}
                    aria-describedby={
                      patternTooLong ? "watchlist-pattern-error" : undefined
                    }
                    placeholder="bc1q… or \\brug ?pull\\b"
                    className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm text-slate-900 shadow-sm focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent/30"
                  />
                  {patternTooLong && (
                    <p
                      id="watchlist-pattern-error"
                      className="mt-1 text-xs font-semibold text-red-700"
                    >
                      Pattern must be {MAX_PATTERN_LENGTH} characters or fewer.
                    </p>
                  )}
                </div>

                <label className="flex items-start gap-2 text-sm text-slate-700">
                  <input
                    type="checkbox"
                    checked={form.isRegex}
                    onChange={(event) =>
                      updateForm("isRegex", event.target.checked)
                    }
                    className="mt-0.5 h-4 w-4 rounded border-slate-300 text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                  />
                  <span>
                    Treat pattern as a regular expression
                    <span className="mt-0.5 block text-xs text-slate-500">
                      Leave unchecked to match the literal text only.
                    </span>
                  </span>
                </label>

                <div>
                  <label
                    htmlFor="watchlist-scope"
                    className="block text-sm font-semibold text-slate-700"
                  >
                    Credential scope (optional)
                  </label>
                  <input
                    id="watchlist-scope"
                    type="text"
                    value={form.credentialId}
                    onChange={(event) =>
                      updateForm("credentialId", event.target.value)
                    }
                    aria-invalid={scopeInvalid}
                    aria-describedby="watchlist-scope-hint"
                    placeholder="Blank for global"
                    className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm text-slate-900 shadow-sm focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent/30"
                  />
                  <p
                    id="watchlist-scope-hint"
                    className={`mt-1 text-xs ${
                      scopeInvalid ? "font-semibold text-red-700" : "text-slate-500"
                    }`}
                  >
                    {scopeInvalid
                      ? "Credential scope must be a valid UUID, or blank for a global rule."
                      : "Paste a credential UUID to scope this rule to one bot, or leave blank for global."}
                  </p>
                </div>

                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <label
                      htmlFor="watchlist-cooldown"
                      className="block text-sm font-semibold text-slate-700"
                    >
                      Cooldown (minutes)
                    </label>
                    <input
                      id="watchlist-cooldown"
                      type="number"
                      min={MIN_COOLDOWN_MINUTES}
                      step={1}
                      value={form.cooldown}
                      onChange={(event) =>
                        updateForm("cooldown", event.target.value)
                      }
                      aria-invalid={cooldownInvalid}
                      aria-describedby={
                        cooldownInvalid ? "watchlist-cooldown-error" : undefined
                      }
                      className="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-sm text-slate-900 shadow-sm focus-visible:border-accent focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-accent/30"
                    />
                    {cooldownInvalid && (
                      <p
                        id="watchlist-cooldown-error"
                        className="mt-1 text-xs font-semibold text-red-700"
                      >
                        Cooldown must be at least {MIN_COOLDOWN_MINUTES} minute.
                      </p>
                    )}
                  </div>
                  <label className="flex items-center gap-2 self-end pb-2 text-sm text-slate-700">
                    <input
                      type="checkbox"
                      checked={form.enabled}
                      onChange={(event) =>
                        updateForm("enabled", event.target.checked)
                      }
                      className="h-4 w-4 rounded border-slate-300 text-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                    />
                    Rule enabled
                  </label>
                </div>

                {validationMessages.length > 0 && (
                  <div
                    role="status"
                    aria-live="polite"
                    className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
                  >
                    Fix the highlighted fields before saving.
                  </div>
                )}

                {formError && (
                  <p
                    role="alert"
                    aria-live="assertive"
                    className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
                  >
                    {formError}
                  </p>
                )}
              </div>

              <div className="flex items-center justify-end gap-2 border-t border-slate-200 bg-slate-50 px-5 py-3">
                <button
                  type="button"
                  onClick={closeForm}
                  disabled={submitting}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-semibold text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={!canSubmit}
                  className="flex items-center gap-1.5 rounded-md bg-ink px-3 py-1.5 text-sm font-bold text-white hover:bg-slate-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {submitting && (
                    <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" />
                  )}
                  {editing ? "Save changes" : "Create rule"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </section>
  );
}
