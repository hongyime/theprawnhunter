import type { NextConfig } from "next";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

// Standalone output is only needed by the Docker runtime image. Producing it
// for local and Vercel builds adds an expensive file-tracing/copying phase.
const isStandaloneBuild = process.env.NEXT_OUTPUT === "standalone";
const frontendRoot = dirname(fileURLToPath(import.meta.url));
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
  // Type checking remains a required gate in `npm test`, Docker, and Vercel.
  // Avoid repeating it inside Next's artifact build, which can exceed the
  // local build timeout on network-backed workspaces.
  typescript: {
    ignoreBuildErrors: true,
  },
  turbopack: {
    root: frontendRoot,
  },
  env: {
    NEXT_PUBLIC_SUPABASE_URL: supabaseUrl,
    NEXT_PUBLIC_SUPABASE_KEY: supabaseAnonKey,
  },
  ...(isStandaloneBuild ? { output: "standalone" as const } : {}),
};

export default nextConfig;

function loadRootEnv(): Record<string, string> {
  const rootEnvPath = join(frontendRoot, "..", ".env");

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
