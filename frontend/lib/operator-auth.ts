import { createClient } from "@supabase/supabase-js";

export const AUTH_NOT_CONFIGURED_REASON = "auth not configured";
export const UNAUTHORIZED_REASON = "unauthorized";
export const FORBIDDEN_REASON = "forbidden";

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/**
 * Gate for same-origin proxies that inject MONITOR_API_KEY.
 *
 * A live Supabase session is not enough: the Auth server is the authority for
 * the admin-controlled `app_metadata.operator` claim, so every request is
 * verified with getUser(token). Fails closed.
 * Returns null when the caller is an operator, otherwise the denial Response.
 */
export async function requireOperator(request: Request): Promise<Response | null> {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL?.trim();
  const key = process.env.NEXT_PUBLIC_SUPABASE_KEY?.trim();
  if (!url || !key) {
    return json({ error: AUTH_NOT_CONFIGURED_REASON }, 503);
  }

  const match = /^Bearer\s+(\S+)$/i.exec(
    request.headers.get("Authorization") ?? "",
  );
  const token = match?.[1];
  if (!token) {
    return json({ error: UNAUTHORIZED_REASON }, 401);
  }

  try {
    const supabase = createClient(url, key, {
      auth: {
        persistSession: false,
        autoRefreshToken: false,
        detectSessionInUrl: false,
      },
    });
    const { data, error } = await supabase.auth.getUser(token);
    if (error || !data.user) {
      return json({ error: UNAUTHORIZED_REASON }, 401);
    }
    if (data.user.app_metadata?.operator !== true) {
      return json({ error: FORBIDDEN_REASON }, 403);
    }
    return null;
  } catch {
    return json({ error: UNAUTHORIZED_REASON }, 401);
  }
}
