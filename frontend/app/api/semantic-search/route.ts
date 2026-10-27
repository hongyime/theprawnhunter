/**
 * Server-only proxy for semantic search.
 *
 * The browser must never receive `MONITOR_API_KEY`: `/monitor/semantic-search`
 * requires it as `X-Monitor-Key`, and any `NEXT_PUBLIC_` copy would be baked
 * into the client bundle. This handler runs on the Next.js server, injects the
 * key from a non-public env var, and forwards only the query fields.
 *
 * Failures never surface as 500s. Every failure path returns HTTP 200 with
 * `fallback: true` so the dashboard can rerun the FTS search and show its
 * existing inline notice.
 */

import { requireOperator } from "@/lib/operator-auth";

export const runtime = "nodejs";

export const SEMANTIC_PROXY_PATH = "/api/semantic-search";
export const SEMANTIC_NOT_CONFIGURED_REASON = "semantic proxy not configured";

type SemanticRequestBody = {
  q?: unknown;
  credential_id?: unknown;
  limit?: unknown;
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fallback(reason: string, code: string): Response {
  return json({ results: [], fallback: true, reason, code });
}

function upstreamUrl(apiBase: string): string {
  return `${apiBase.replace(/\/+$/, "")}/monitor/semantic-search`;
}

export async function POST(request: Request): Promise<Response> {
  // Only authenticated operators may reach the monitor proxy.
  const denied = await requireOperator(request);
  if (denied) return denied;
  const apiBase = (process.env.API_BASE_URL || "").trim();
  const monitorKey = (process.env.MONITOR_API_KEY || "").trim();

  if (!apiBase || !monitorKey) {
    console.warn("semantic-search proxy not configured");
    return fallback(SEMANTIC_NOT_CONFIGURED_REASON, "not_configured");
  }

  let rawBody: unknown;
  try {
    rawBody = await request.json();
  } catch {
    console.warn("semantic-search proxy rejected a malformed body");
    return fallback("invalid request body", "invalid_request");
  }

  const body = (rawBody ?? {}) as SemanticRequestBody;
  const query = typeof body.q === "string" ? body.q.trim() : "";
  if (!query) {
    return fallback("empty query", "empty_query");
  }

  const payload: Record<string, unknown> = { q: query };
  if (typeof body.credential_id === "string" && body.credential_id) {
    payload.credential_id = body.credential_id;
  }
  if (typeof body.limit === "number" && Number.isFinite(body.limit)) {
    payload.limit = body.limit;
  }

  try {
    const upstream = await fetch(upstreamUrl(apiBase), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Monitor-Key": monitorKey,
      },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: AbortSignal.timeout(35_000),
    });

    if (!upstream.ok) {
      // Never relay the upstream body: it is untrusted and may contain details
      // the dashboard should not see. The status alone is enough for the log.
      console.warn(
        `semantic-search upstream returned ${upstream.status}`,
      );
      return fallback(
        `semantic proxy upstream ${upstream.status}`,
        "upstream_error",
      );
    }

    let data: unknown;
    try {
      data = await upstream.json();
    } catch {
      console.warn("semantic-search upstream returned invalid JSON");
      return fallback(
        "semantic proxy invalid response",
        "upstream_invalid_response",
      );
    }

    // Forward the upstream payload unchanged; this route adds nothing to it.
    return json(data);
  } catch (error) {
    if (
      error instanceof Error &&
      (error.name === "TimeoutError" || error.name === "AbortError")
    ) {
      console.warn("semantic-search upstream timed out");
      return fallback("semantic proxy upstream timeout", "upstream_timeout");
    }
    console.warn("semantic-search upstream unreachable");
    return fallback("semantic proxy upstream unreachable", "upstream_unreachable");
  }
}
