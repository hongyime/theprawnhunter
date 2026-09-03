"use client";

import { useState, useEffect } from "react";
import Sidebar from "@/components/Sidebar";
import ChatWindow from "@/components/ChatWindow";
import TelemetryAnalyticsView from "@/components/TelemetryAnalyticsView";
import { LucideMenu, LucideX } from "lucide-react";

export type DashboardView = "chat" | "botTelemetry" | "globalTelemetry";

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
  const [selected, setSelected] = useState<Credential | null>(null);
  const [activeView, setActiveView] = useState<DashboardView>("chat");
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
              {selected?.meta?.bot_username
                ? `@${selected.meta.bot_username}`
                : "Prawn Hunter"}
            </span>
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
        {activeView === "globalTelemetry" ? (
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
