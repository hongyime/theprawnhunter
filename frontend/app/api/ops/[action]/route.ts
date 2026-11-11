/**
 * Server-only proxy for pause / resume / status operations.
 *
 * The browser must never receive `MONITOR_API_KEY`: every `/monitor/ops/*`
 * upstream requires it as `X-Monitor-Key`, and any `NEXT_PUBLIC_` copy would be
 * baked into the client bundle. This handler runs on the Next.js server,
 * injects the key from the non-public env var, and returns only a short JSON
 * error when configuration or the upstream is unavailable.
 */

import { requireOperator } from "@/lib/operator-auth";

export const runtime = "nodejs";

export const OPS_NOT_CONFIGURED_REASON = "ops proxy not configured";

const ACTION_METHODS = {
  status: "GET",
  pause: "POST",
  resume: "POST",
  "retry-failed-broadcasts": "POST",
  "circuit-breakers": "GET",
  "reset-circuit-breaker": "POST",
  scan: "POST",
} as const;

type OpsAction = keyof typeof ACTION_METHODS;

type RouteContext = { params: Promise<{ action: string }> };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(
  reason: string,
  status: number,
  code: string,
): Response {
  return json({ error: reason, code }, status);
}

function upstreamUrl(apiBase: string, action: OpsAction, body?: Record<string, string>): string {
  const base = apiBase.replace(/\/+$/, "");
  if (action === "circuit-breakers") return `${base}/health/circuit-breakers`;
  if (action === "reset-circuit-breaker") return `${base}/health/circuit-breakers/${encodeURIComponent(body!.service)}/reset`;
  if (action === "scan") return `${base}/scan/trigger`;
  return `${base}/monitor/ops/${action}`;
}

async function proxy(action: OpsAction, body?: Record<string, string>): Promise<Response> {
  const apiBase = (process.env.API_BASE_URL || "").trim();
  const monitorKey = (process.env.MONITOR_API_KEY || "").trim();

  if (!apiBase || !monitorKey) {
    console.warn("ops proxy not configured");
    return errorResponse(OPS_NOT_CONFIGURED_REASON, 503, "not_configured");
  }

  try {
    const upstream = await fetch(upstreamUrl(apiBase, action, body), {
      method: ACTION_METHODS[action],
      headers: {
        Accept: "application/json",
        "X-Monitor-Key": monitorKey,
        ...(body ? { "Content-Type": "application/json" } : {}),
      },
      ...(body ? { body: JSON.stringify(body) } : {}),
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });

    if (!upstream.ok) {
      // Never relay the upstream body: it is untrusted and the status alone is
      // enough for the client to roll back its optimistic state.
      console.warn(`ops/${action} upstream returned ${upstream.status}`);
      return errorResponse(
        `ops upstream ${upstream.status}`,
        upstream.status,
        "upstream_error",
      );
    }

    let data: unknown;
    try {
      data = await upstream.json();
    } catch {
      console.warn(`ops/${action} upstream returned invalid JSON`);
      return errorResponse(
        "ops upstream invalid response",
        502,
        "upstream_invalid_response",
      );
    }

    return json(data);
  } catch (error) {
    const name = error instanceof Error ? error.name : "";
    if (name === "TimeoutError" || name === "AbortError") {
      console.warn(`ops/${action} upstream timed out`);
      return errorResponse("ops upstream timeout", 504, "upstream_timeout");
    }
    console.warn(`ops/${action} upstream unreachable`);
    return errorResponse(
      "ops upstream unreachable",
      502,
      "upstream_unreachable",
    );
  }
}

async function resolveAction(context: RouteContext): Promise<string> {
  const params = await context.params;
  return params.action;
}

export async function GET(
  request: Request,
  context: RouteContext,
): Promise<Response> {
  // Only authenticated operators may reach the monitor proxy.
  const denied = await requireOperator(request);
  if (denied) return denied;
  const action = await resolveAction(context);
  if (action === "status" || action === "circuit-breakers") {
    return proxy(action);
  }
  if (action === "pause" || action === "resume" || action === "retry-failed-broadcasts" || action === "reset-circuit-breaker" || action === "scan") {
    return errorResponse("method not allowed", 405, "method_not_allowed");
  }
  return errorResponse("unknown ops action", 404, "unknown_action");
}

export async function POST(
  request: Request,
  context: RouteContext,
): Promise<Response> {
  // Only authenticated operators may reach the monitor proxy.
  const denied = await requireOperator(request);
  if (denied) return denied;
  const action = await resolveAction(context);
  if (action === "pause" || action === "resume" || action === "retry-failed-broadcasts") {
    return proxy(action);
  }
  if (action === "reset-circuit-breaker" || action === "scan") {
    let body: unknown;
    try { body = await request.json(); } catch { return errorResponse("invalid request body", 400, "invalid_body"); }
    if (!body || typeof body !== "object") return errorResponse("invalid request body", 400, "invalid_body");
    if (action === "reset-circuit-breaker") {
      const service = (body as { service?: unknown }).service;
      if (typeof service !== "string" || !/^[a-z0-9_-]+$/i.test(service)) return errorResponse("invalid service", 400, "invalid_body");
      return proxy(action, { service });
    }
    const { source, query } = body as { source?: unknown; query?: unknown };
    const sources = ["shodan", "fofa", "github", "urlscan", "sourcegraph", "searchcode"];
    if (typeof source !== "string" || !sources.includes(source) || typeof query !== "string" || !query.trim()) return errorResponse("invalid scan request", 400, "invalid_body");
    return proxy(action, { source, query: query.trim() });
  }
  if (action === "status" || action === "circuit-breakers") {
    return errorResponse("method not allowed", 405, "method_not_allowed");
  }
  return errorResponse("unknown ops action", 404, "unknown_action");
}
