/**
 * Server-only proxy for pause / resume / status operations.
 *
 * The browser must never receive `MONITOR_API_KEY`: every `/monitor/ops/*`
 * upstream requires it as `X-Monitor-Key`, and any `NEXT_PUBLIC_` copy would be
 * baked into the client bundle. This handler runs on the Next.js server,
 * injects the key from the non-public env var, and returns only a short JSON
 * error when configuration or the upstream is unavailable.
 */

export const runtime = "nodejs";

export const OPS_NOT_CONFIGURED_REASON = "ops proxy not configured";

const ACTION_METHODS = {
  status: "GET",
  pause: "POST",
  resume: "POST",
} as const;

type OpsAction = keyof typeof ACTION_METHODS;

type RouteContext = { params: Promise<{ action: string }> };

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function errorResponse(reason: string, status: number): Response {
  return json({ error: reason }, status);
}

function upstreamUrl(apiBase: string, action: OpsAction): string {
  return `${apiBase.replace(/\/+$/, "")}/monitor/ops/${action}`;
}

async function proxy(action: OpsAction): Promise<Response> {
  const apiBase = (process.env.API_BASE_URL || "").trim();
  const monitorKey = (process.env.MONITOR_API_KEY || "").trim();

  if (!apiBase || !monitorKey) {
    console.warn("ops proxy not configured");
    return errorResponse(OPS_NOT_CONFIGURED_REASON, 503);
  }

  try {
    const upstream = await fetch(upstreamUrl(apiBase, action), {
      method: ACTION_METHODS[action],
      headers: {
        Accept: "application/json",
        "X-Monitor-Key": monitorKey,
      },
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });

    if (!upstream.ok) {
      // Never relay the upstream body: it is untrusted and the status alone is
      // enough for the client to roll back its optimistic state.
      console.warn(`ops/${action} upstream returned ${upstream.status}`);
      return errorResponse(`ops upstream ${upstream.status}`, upstream.status);
    }

    let data: unknown;
    try {
      data = await upstream.json();
    } catch {
      console.warn(`ops/${action} upstream returned invalid JSON`);
      return errorResponse("ops upstream invalid response", 502);
    }

    return json(data);
  } catch {
    console.warn(`ops/${action} upstream unreachable`);
    return errorResponse("ops upstream unreachable", 502);
  }
}

async function resolveAction(context: RouteContext): Promise<string> {
  const params = await context.params;
  return params.action;
}

export async function GET(
  _request: Request,
  context: RouteContext,
): Promise<Response> {
  const action = await resolveAction(context);
  if (action === "status") {
    return proxy("status");
  }
  if (action === "pause" || action === "resume") {
    return errorResponse("method not allowed", 405);
  }
  return errorResponse("unknown ops action", 404);
}

export async function POST(
  _request: Request,
  context: RouteContext,
): Promise<Response> {
  const action = await resolveAction(context);
  if (action === "pause" || action === "resume") {
    return proxy(action);
  }
  if (action === "status") {
    return errorResponse("method not allowed", 405);
  }
  return errorResponse("unknown ops action", 404);
}
