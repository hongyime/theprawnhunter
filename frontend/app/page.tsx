"use client";

import { useState, useEffect } from "react";
import Sidebar from "@/components/Sidebar";
import ChatWindow from "@/components/ChatWindow";
import FindingsQueue from "@/components/FindingsQueue";
import SearchView from "@/components/SearchView";
import TelemetryAnalyticsView from "@/components/TelemetryAnalyticsView";
import WatchlistView from "@/components/WatchlistView";
import WatchlistHitsView from "@/components/WatchlistHitsView";
import OpsPanel from "@/components/OpsPanel";
import DashboardView from "@/components/DashboardView";
import { LucideMenu, LucideX, LogOut } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { supabase } from "@/lib/supabase";
import NeoCard from "@/components/NeoCard";

export type DashboardView =
  | "findings"
  | "search"
  | "watchlists"
  | "watchlistHits"
  | "ops"
  | "chat"
  | "botTelemetry"
  | "globalTelemetry"
  | "dashboard";

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
  chat_id?: number | null;
  chat_name?: string | null;
  chat_type?: string | null;
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

  const showTargetsPanel = activeView === "botTelemetry" || activeView === "chat";

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
        "id,created_at,source,meta,confidence_score,collection_yield_score,chat_member_count,chat_id,chat_name,chat_type",
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
        <NeoCard className="p-6">
          <div className="text-slate-600">Authenticating...</div>
        </NeoCard>
      </div>
    );
  }

  // Gate: redirect to sign-in if not authenticated
  if (!session) {
    return (
      <div className="flex h-screen flex-col items-center justify-center bg-slate-100 gap-4">
        <NeoCard className="p-8 text-center">
          <h1 className="text-2xl font-bold text-slate-900 mb-2">The Prawn Hunter</h1>
          <p className="text-slate-600 mb-4">
            {authError || "Sign in required to access the dashboard"}
          </p>
          <a
            href="/signin"
            className="inline-block px-6 py-2 bg-accent text-white rounded-md hover:bg-accent-strong font-medium"
          >
            Sign In
          </a>
        </NeoCard>
      </div>
    );
  }

  return (
    <main className="flex h-screen w-full flex-col overflow-hidden bg-paper">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b border-slate-200 bg-white px-3 md:px-4">
        {isMobile && (
          <button
            type="button"
            onClick={() => setSidebarOpen((v) => !v)}
            className="rounded-md p-1.5 text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
            aria-label={sidebarOpen ? "Close menu" : "Open menu"}
          >
            {sidebarOpen ? <LucideX className="h-5 w-5" /> : <LucideMenu className="h-5 w-5" />}
          </button>
        )}
        <div
          className="flex items-center gap-2"
          title="Authenticated operator session"
        >
          <span className="text-sm font-medium text-slate-700">
            Operator session
          </span>
          <span className="rounded bg-slate-200 px-1.5 py-0.5 font-mono text-xs font-bold uppercase tracking-[0.16em] text-slate-600">
            OPERATOR
          </span>
        </div>
        <button
          onClick={handleSignOut}
          className="ml-auto flex items-center gap-2 rounded-md px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
          aria-label="Sign Out"
        >
          <LogOut className="h-4 w-4" />
          <span className="hidden sm:inline">Sign Out</span>
        </button>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {isMobile && sidebarOpen && (
          <div
            className="fixed inset-x-0 bottom-0 top-12 z-20 bg-ink/40"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar: fixed drawer on mobile below the app header, static column on desktop */}
        <div
          className={
            isMobile
              ? `fixed bottom-0 left-0 top-12 z-30 flex transform bg-white shadow-xl transition-transform duration-200 ${
                  showTargetsPanel ? "w-[88vw] max-w-sm" : "w-auto"
                } ${sidebarOpen ? "translate-x-0" : "-translate-x-full"}`
              : showTargetsPanel
              ? "w-1/3 min-w-75 shrink-0"
              : "shrink-0"
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
            onSignOut={handleSignOut}
          />
        </div>

        {/* Tab content sits below the global operator header */}
        <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
          {activeView === "findings" ? (
            <FindingsQueue onDrilldown={handleFindingDrilldown} />
          ) : activeView === "search" ? (
            <SearchView
              onOpenChat={(credentialId) =>
                handleFindingDrilldown(credentialId, "chat")
              }
            />
          ) : activeView === "watchlists" ? (
            <WatchlistView />
          ) : activeView === "watchlistHits" ? (
            <WatchlistHitsView />
          ) : activeView === "ops" ? (
            <OpsPanel />
          ) : activeView === "dashboard" ? (
            <DashboardView />
          ) : activeView === "globalTelemetry" ? (
            <TelemetryAnalyticsView scope="global" />
          ) : activeView === "botTelemetry" ? (
            <TelemetryAnalyticsView scope="credential" credential={selected} />
          ) : (
            <ChatWindow credential={selected} />
          )}
        </div>
      </div>
    </main>
  );
}
