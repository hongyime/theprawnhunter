"use client";

import { useState, useEffect } from "react";
import Sidebar from "@/components/Sidebar";
import ChatWindow from "@/components/ChatWindow";
import FindingsQueue from "@/components/FindingsQueue";
import TelemetryAnalyticsView from "@/components/TelemetryAnalyticsView";
import { LucideMenu, LucideX, LogOut } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { supabase } from "@/lib/supabase";

export type DashboardView = "findings" | "chat" | "botTelemetry" | "globalTelemetry";

export type GatewayTelemetry = {
  configured_webhook_url?: string | null;
  resolved_ip_address?: string | null;
  command_dictionary?: Array<{ command?: string | null; description?: string | null }>;
  error_profile?: unknown;
  last_error_info?: string | null;
};

export type InfrastructureContext = {
  source_file_path?: string | null;
  repository_uri?: string | null;
  co_located_endpoints?: string[];
  [key: string]: unknown;
};

export interface Credential {
  id: string;
  created_at: string;
  source: string;
  collection_yield_score?: number | null;
  /** @deprecated legacy alias for collection_yield_score */
  confidence_score?: number | null;
  chat_member_count?: number | null;
  meta?: {
    chat_title?: string;
    bot_username?: string;
    bot_id?: string;
    gateway_telemetry?: GatewayTelemetry;
    infrastructure_context?: InfrastructureContext;
    [key: string]: unknown;
  };
}

export default function Home() {
  const { session, loading: authLoading, error: authError, signOut } = useAuth();
  const [selected, setSelected] = useState<Credential | null>(null);
  const [activeView, setActiveView] = useState<DashboardView>("findings");
  const [isMobile, setIsMobile] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    const checkMobile = () => {
      setIsMobile(window.innerWidth < 768);
    };
    checkMobile();
    window.addEventListener("resize", checkMobile);
    return () => window.removeEventListener("resize", checkMobile);
  }, []);

  const handleSelect = (cred: Credential) => {
    setSelected(cred);
    setActiveView((currentView) =>
      currentView === "botTelemetry" ? "botTelemetry" : "chat"
    );
    if (isMobile) setSidebarOpen(false);
  };

  const handleSignOut = async () => {
    await signOut();
  };

  const handleFindingDrilldown = async (
    credentialId: string,
    view: Extract<DashboardView, "chat" | "botTelemetry">,
  ) => {
    const { data, error } = await supabase
      .from("discovered_credentials_public")
      .select(
        "id,created_at,source,meta,confidence_score,collection_yield_score,chat_member_count",
      )
      .eq("id", credentialId)
      .limit(1);
    if (!error && data?.[0]) {
      setSelected(data[0] as Credential);
      setActiveView(view);
    }
  };

  // Show loading state
  if (authLoading) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-100">
        <div className="text-slate-600">Authenticating...</div>
      </div>
    );
  }

  // Gate: redirect to sign-in if not authenticated
  if (!session) {
    return (
      <div className="flex h-screen flex-col items-center justify-center bg-slate-100 gap-4">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-slate-900 mb-2">Telegram Hunter</h1>
          <p className="text-slate-600 mb-4">
            {authError || "Sign in required to access the dashboard"}
          </p>
          <a
            href="/signin"
            className="inline-block px-6 py-2 bg-cyan-600 text-white rounded-md hover:bg-cyan-700 font-medium"
          >
            Sign In
          </a>
        </div>
      </div>
    );
  }

  return (
    <main className="flex h-screen w-full overflow-hidden bg-white">
      {isMobile && (
        <>
          {/* Mobile top bar */}
          <div className="fixed top-0 left-0 right-0 z-30 flex items-center gap-2 border-b bg-white px-3 py-2 shadow-sm">
            <button
              type="button"
              onClick={() => setSidebarOpen((v) => !v)}
              className="rounded p-1.5 text-slate-700 hover:bg-slate-100"
              aria-label={sidebarOpen ? "Close menu" : "Open menu"}
            >
              {sidebarOpen ? <LucideX className="h-5 w-5" /> : <LucideMenu className="h-5 w-5" />}
            </button>
            <span className="truncate text-sm font-semibold text-slate-800">
              {activeView === "findings"
                ? "Findings queue"
                : selected?.meta?.bot_username
                ? `@${selected.meta.bot_username}`
                : "Prawn Hunter"}
            </span>
            <button
              onClick={handleSignOut}
              className="ml-auto rounded p-1.5 text-slate-700 hover:bg-slate-100"
              aria-label="Sign out"
            >
              <LogOut className="h-5 w-5" />
            </button>
          </div>
          {/* Backdrop */}
          {sidebarOpen && (
            <div
              className="fixed inset-0 z-20 bg-black/40"
              onClick={() => setSidebarOpen(false)}
            />
          )}
        </>
      )}

      {/* Sidebar: fixed drawer on mobile, static column on desktop */}
      <div
        className={
          isMobile
            ? `fixed inset-y-0 left-0 z-30 w-4/5 max-w-xs transform bg-white shadow-xl transition-transform duration-200 ${
                sidebarOpen ? "translate-x-0" : "-translate-x-full"
              }`
            : "w-1/3 min-w-75 shrink-0"
        }
      >
        <Sidebar
          selected={selected}
          activeView={activeView}
          onViewChange={(v) => {
            setActiveView(v);
            if (isMobile) setSidebarOpen(false);
          }}
          onSelect={handleSelect}
        />
      </div>

      {/* Main content */}
      <div
        className={`flex flex-1 flex-col overflow-hidden ${
          isMobile ? "pt-12" : ""
        }`}
      >
        {/* Desktop header with sign-out */}
        {!isMobile && (
          <div className="flex items-center justify-between border-b px-4 py-2 bg-slate-50">
            <span className="text-sm text-slate-600">
              Signed in as <strong>{session.user?.email}</strong>
            </span>
            <button
              onClick={handleSignOut}
              className="flex items-center gap-2 rounded px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-200"
            >
              <LogOut className="h-4 w-4" />
              Sign Out
            </button>
          </div>
        )}
        
        {activeView === "findings" ? (
          <FindingsQueue onDrilldown={handleFindingDrilldown} />
        ) : activeView === "globalTelemetry" ? (
          <TelemetryAnalyticsView scope="global" />
        ) : activeView === "botTelemetry" ? (
          <TelemetryAnalyticsView scope="credential" credential={selected} />
        ) : (
          <ChatWindow credential={selected} />
        )}
      </div>
    </main>
  );
}
