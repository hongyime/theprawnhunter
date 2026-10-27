/**
 * Server-only proxy for a watchlist hit's Telegram deep link.
 *
 * The browser must never receive `MONITOR_API_KEY`: the monitor endpoint
 * requires it as `X-Monitor-Key`, and any `NEXT_PUBLIC_` copy would be baked
 * into the client bundle. This handler runs on the Next.js server, injects the
 * key from a non-public env var, and forwards only the hit id.
 *
 * The upstream resolver owns the group/topic logic (`t.me/c/...` needs the
 * numeric supergroup and forum topic ids), so this route never tries to build
 * a link from the hit row itself.
 */

import { requireOperator } from "@/lib/operator-auth";

export const runtime = "nodejs";

export const WATCHLIST_HIT_LINK_PATH = "/api/watchlist-hit-link";

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function upstreamUrl(apiBase: string, hitId: string): string {
  return `${apiBase.replace(/\/+$/, "")}/monitor/watchlist/hits/${encodeURIComponent(
    hitId,
  )}/link`;
}

export async function GET(request: Request): Promise<Response> {
  // Only authenticated operators may reach the monitor proxy.
  const denied = await requireOperator(request);
  if (denied) return denied;
  const apiBase = (process.env.API_BASE_URL || "").trim();
  const monitorKey = (process.env.MONITOR_API_KEY || "").trim();

  if (!apiBase || !monitorKey) {
    console.warn("watchlist-hit-link proxy not configured");
    return json({ url: null });
  }

  const hitId = new URL(request.url).searchParams.get("hit_id")?.trim() ?? "";
  if (!UUID_PATTERN.test(hitId)) {
    return json({ url: null }, 400);
  }

  try {
    const upstream = await fetch(upstreamUrl(apiBase, hitId), {
      method: "GET",
      headers: { "X-Monitor-Key": monitorKey },
      cache: "no-store",
      signal: AbortSignal.timeout(15_000),
    });

    if (upstream.status === 404) {
      return json({ url: null }, 404);
    }
    if (!upstream.ok) {
      // Never relay the upstream body: it is untrusted and may contain details
      // the dashboard should not see. The status alone is enough for the log.
      console.warn(`watchlist-hit-link upstream returned ${upstream.status}`);
      return json({ url: null });
    }

    let data: unknown;
    try {
      data = await upstream.json();
    } catch {
      console.warn("watchlist-hit-link upstream returned invalid JSON");
      return json({ url: null });
    }

    const url =
      data && typeof data === "object" && "url" in data
        ? (data as { url?: unknown }).url
        : null;

    return json({ url: typeof url === "string" && url ? url : null });
  } catch {
    console.warn("watchlist-hit-link upstream unreachable");
    return json({ url: null });
  }
}
