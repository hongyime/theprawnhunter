import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// Belt and braces: if the landing route ever imports a data client, these
// spies will catch the call at render time.
const mockCreateClient = vi.hoisted(() => vi.fn(() => ({ auth: {} })));
const mockFrom = vi.hoisted(() => vi.fn());
const mockFetch = vi.hoisted(() => vi.fn());

vi.mock("@supabase/supabase-js", () => ({ createClient: mockCreateClient }));
vi.mock("@/lib/supabase", () => ({
  supabase: {
    auth: {
      getSession: vi.fn(),
      onAuthStateChange: vi.fn(),
    },
    from: mockFrom,
  },
}));

import AboutPage from "./page";

// The strongest guarantee: read the route module itself and prove it has no
// data-client import, no network primitive, and no client-side directive.
const SOURCE = readFileSync(
  resolve(process.cwd(), "app/about/page.tsx"),
  "utf8",
);

const PIPELINE_HEADINGS = ["Discover", "Validate", "Harvest", "Deliver"];

const FEATURE_HEADINGS = [
  "Findings queue",
  "Full-text and semantic search",
  "Chat triage",
  "Watchlists and alerts",
  "Telemetry",
  "Self-hosted control",
];

describe("About landing page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubGlobal("fetch", mockFetch);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders the hero copy", () => {
    render(<AboutPage />);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(
      /exposed telegram bots/i,
    );
    expect(screen.getByText("Self-hosted OSINT pipeline")).toBeInTheDocument();
    expect(
      screen.getByText(/discovers leaked Telegram Bot API tokens/i),
    ).toBeInTheDocument();
  });

  it("renders the four pipeline stages", () => {
    render(<AboutPage />);

    expect(
      screen.getByRole("heading", { name: /four stages/i }),
    ).toBeInTheDocument();
    for (const heading of PIPELINE_HEADINGS) {
      expect(
        screen.getByRole("heading", { name: heading }),
      ).toBeInTheDocument();
    }
  });

  it("renders the six operator feature headings", () => {
    render(<AboutPage />);

    for (const heading of FEATURE_HEADINGS) {
      expect(
        screen.getByRole("heading", { name: heading }),
      ).toBeInTheDocument();
    }
  });

  it("renders the private-by-design section and licence notice", () => {
    render(<AboutPage />);

    expect(
      screen.getByRole("heading", { name: "Private by design" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Apache-2\.0/)).toBeInTheDocument();
  });

  it("has exactly one Sign in link to /signin and a secondary link to /", () => {
    render(<AboutPage />);

    const signIn = screen.getByRole("link", { name: /^sign in$/i });
    expect(signIn).toHaveAttribute("href", "/signin");

    const signInLinks = screen
      .getAllByRole("link")
      .filter((link) => link.getAttribute("href") === "/signin");
    expect(signInLinks).toHaveLength(1);

    expect(
      screen.getByRole("link", { name: /back to the app/i }),
    ).toHaveAttribute("href", "/");
  });

  it("makes no Supabase or network calls when rendered", () => {
    render(<AboutPage />);

    expect(mockCreateClient).not.toHaveBeenCalled();
    expect(mockFrom).not.toHaveBeenCalled();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("imports no data client and performs no network access at module scope", () => {
    expect(SOURCE).not.toMatch(
      /lib\/supabase|@supabase|fetch\(|XMLHttpRequest|axios/i,
    );
    expect(SOURCE).not.toMatch(/^["']use client["']/m);
    expect(SOURCE).not.toMatch(/\/monitor\//);
  });
});
