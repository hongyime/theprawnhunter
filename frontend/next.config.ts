import type { NextConfig } from "next";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// output: 'standalone' is required for Docker builds (existing docker-compose service).
// On Vercel, we want the default output so their build pipeline works natively.
// The VERCEL env var is auto-set to '1' by Vercel's build environment.
const isVercel = process.env.VERCEL === "1";
const rootEnv = loadRootEnv();

const supabaseUrl =
  process.env.NEXT_PUBLIC_SUPABASE_URL ||
  process.env.SUPABASE_URL ||
  rootEnv.SUPABASE_URL;
const supabaseAnonKey =
  process.env.NEXT_PUBLIC_SUPABASE_KEY ||
  process.env.SUPABASE_KEY ||
  rootEnv.SUPABASE_KEY;

const nextConfig: NextConfig = {
  reactCompiler: true,
  env: {
    NEXT_PUBLIC_SUPABASE_URL: supabaseUrl,
    NEXT_PUBLIC_SUPABASE_KEY: supabaseAnonKey,
  },
  ...(isVercel ? {} : { output: "standalone" as const }),
};

export default nextConfig;

function loadRootEnv(): Record<string, string> {
  const configDir = dirname(fileURLToPath(import.meta.url));
  const rootEnvPath = join(configDir, "..", ".env");

  if (!existsSync(rootEnvPath)) {
    return {};
  }

  return readFileSync(rootEnvPath, "utf8")
    .split(/\r?\n/)
    .reduce<Record<string, string>>((values, line) => {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith("#")) {
        return values;
      }

      const separatorIndex = trimmed.indexOf("=");
      if (separatorIndex === -1) {
        return values;
      }

      const key = trimmed.slice(0, separatorIndex).trim();
      if (key !== "SUPABASE_URL" && key !== "SUPABASE_KEY") {
        return values;
      }

      values[key] = trimmed.slice(separatorIndex + 1).trim().replace(/^["']|["']$/g, "");
      return values;
    }, {});
}
