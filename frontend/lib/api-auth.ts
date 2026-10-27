import { supabase } from "./supabase";

/** Bearer header for same-origin proxies; empty when signed out or expired. */
export async function authHeaders(): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  return token ? { Authorization: `Bearer ${token}` } : {};
}
