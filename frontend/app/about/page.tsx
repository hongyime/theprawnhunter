import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "About · The Prawn Hunter",
  description:
    "A self-hosted OSINT pipeline that discovers exposed Telegram Bot API tokens, validates them against the live Telegram API, harvests accessible chat history, and delivers evidence to a private operator dashboard.",
};

const PIPELINE = [
  {
    index: "01",
    title: "Discover",
    detail: "21 public sources",
    body: "Scanners sweep code hosts, paste sites, web indexes, and scanner APIs for exposed Telegram Bot API tokens.",
  },
  {
    index: "02",
    title: "Validate",
    detail: "Live Telegram API",
    body: "Each candidate is checked against the live Telegram API, so dead, revoked, and fabricated tokens never reach the queue.",
  },
  {
    index: "03",
    title: "Harvest",
    detail: "Telethon + Bot API",
    body: "Valid bots are scraped for whatever chat history remains accessible through Telethon and the Bot API.",
  },
  {
    index: "04",
    title: "Deliver",
    detail: "Supergroup + dashboard",
    body: "Findings land as per-bot forum topics in a private supergroup and as structured evidence in the dashboard.",
  },
] as const;

const FEATURES = [
  {
    title: "Findings queue",
    body: "Every validated bot enters a prioritised analyst queue with collection-yield scoring, status, and feedback-driven triage.",
  },
  {
    title: "Full-text and semantic search",
    body: "Search the harvested corpus with Postgres full-text by default, or switch to semantic search when the embedding service is configured.",
  },
  {
    title: "Chat triage",
    body: "Read the accessible message history for any bot in the chat feed and open the exact evidence behind a finding.",
  },
  {
    title: "Watchlists and alerts",
    body: "Track keywords, route matching hits to a dedicated Telegram topic, and forward alerts to the webhook of your choice.",
  },
  {
    title: "Telemetry",
    body: "Inspect per-bot gateway telemetry, infrastructure context, and fleet-wide collection health.",
  },
  {
    title: "Self-hosted control",
    body: "Run the whole pipeline on your own Docker Compose stack, Supabase project, and Redis. Nothing is published publicly.",
  },
] as const;

const PRIVACY = [
  {
    title: "Operator-only dashboard",
    body: "Every dashboard session is checked against an administrator-controlled operator claim before any data is returned.",
  },
  {
    title: "No public data",
    body: "Evidence goes to a private supergroup and a gated dashboard. There is no public feed, gallery, or dataset to browse.",
  },
  {
    title: "No third-party analytics",
    body: "No analytics SDKs, tracking cookies, or external assets. Findings stay inside infrastructure you control.",
  },
] as const;

const SECTION_LABEL =
  "font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-slate-500";
const CARD = "rounded-lg border border-slate-200 bg-white";
const PRIMARY_BUTTON =
  "inline-flex items-center justify-center rounded-md bg-[#197e74] px-4 py-2.5 text-sm font-bold text-white shadow-sm transition-colors hover:bg-[#12645c] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]";
const SECONDARY_BUTTON =
  "inline-flex items-center justify-center rounded-md border border-slate-300 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 transition-colors hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]";

function Mark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="8.25" />
      <circle cx="12" cy="12" r="3.25" />
      <path d="M12 3.75v3M12 17.25v3M3.75 12h3M17.25 12h3" strokeLinecap="round" />
    </svg>
  );
}

function SectionHeading({
  id,
  eyebrow,
  title,
}: {
  id: string;
  eyebrow: string;
  title: string;
}) {
  return (
    <div className="max-w-2xl">
      <p className={SECTION_LABEL}>{eyebrow}</p>
      <h2
        id={id}
        className="mt-2 text-2xl font-bold tracking-tight text-slate-900"
      >
        {title}
      </h2>
    </div>
  );
}

export default function AboutPage() {
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 sm:px-6">
          <div className="flex min-w-0 items-center gap-2">
            <Mark className="h-5 w-5 shrink-0 text-[#197e74]" />
            <span className="truncate text-sm font-bold text-slate-800">
              The Prawn Hunter
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="hidden rounded bg-slate-200 px-1.5 py-0.5 font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-slate-600 md:inline">
              Operator console
            </span>
            <a
              href="/"
              className="inline-flex items-center justify-center rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-slate-700 transition-colors hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
            >
              Back to the app
            </a>
            <a
              href="/signin"
              className="inline-flex items-center justify-center rounded-md bg-[#197e74] px-2.5 py-1.5 text-xs font-bold text-white shadow-sm transition-colors hover:bg-[#12645c] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#197e74]"
            >
              Sign in
            </a>
          </div>
        </div>
      </header>

      <main>
        <section aria-labelledby="hero-heading">
          <div className="mx-auto max-w-6xl px-4 pb-12 pt-12 sm:px-6 sm:pb-16 sm:pt-16">
            <p className={SECTION_LABEL}>Self-hosted OSINT pipeline</p>
            <h1
              id="hero-heading"
              className="mt-3 max-w-3xl text-3xl font-bold tracking-tight text-slate-900"
            >
              Exposed Telegram bots, found, proven, kept private.
            </h1>
            <p className="mt-4 max-w-4xl text-base leading-7 text-slate-600">
              The Prawn Hunter discovers leaked Telegram Bot API tokens across
              public sources, validates each one against the live Telegram API,
              and delivers the evidence to a private operator supergroup and
              dashboard.
            </p>
            <div className="mt-7 flex flex-col gap-3 sm:flex-row sm:items-center">
              <a href="#steps" className={PRIMARY_BUTTON}>
                How it works
              </a>
              <a href="#privacy" className={SECONDARY_BUTTON}>
                Private by design
              </a>
            </div>
            <p className={`mt-6 ${SECTION_LABEL}`}>Operator access only</p>
          </div>
        </section>

        <section
          aria-labelledby="steps-heading"
          className="border-t border-slate-200"
        >
          <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 sm:py-16">
            <SectionHeading
              id="steps-heading"
              eyebrow="How it works"
              title="Four stages, one private pipeline."
            />
            <ol className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {PIPELINE.map((step) => (
                <li key={step.index} className={`${CARD} p-5`}>
                  <span className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-[#197e74]">
                    {step.index}
                  </span>
                  <h3 className="mt-2 text-sm font-semibold text-slate-900">
                    {step.title}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-slate-600">
                    {step.body}
                  </p>
                  <p className="mt-4 font-mono text-[10px] uppercase tracking-[0.16em] text-slate-500">
                    {step.detail}
                  </p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section
          aria-labelledby="features-heading"
          className="border-t border-slate-200"
        >
          <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 sm:py-16">
            <SectionHeading
              id="features-heading"
              eyebrow="Operator surfaces"
              title="Built for an operator, not a crowd."
            />
            <div className="mt-8 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {FEATURES.map((feature, index) => (
                <article
                  key={feature.title}
                  className={`${CARD} p-5 shadow-[0_1px_0_rgba(23,32,51,0.04)]`}
                >
                  <span className="font-mono text-[10px] font-bold uppercase tracking-[0.16em] text-slate-500">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <h3 className="mt-2 text-sm font-semibold text-slate-900">
                    {feature.title}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-slate-600">
                    {feature.body}
                  </p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section
          aria-labelledby="privacy-heading"
          className="border-y border-slate-200 bg-white"
        >
          <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 sm:py-16">
            <h2
              id="privacy-heading"
              className="text-2xl font-bold tracking-tight text-slate-900"
            >
              Private by design
            </h2>
            <div className="mt-6 grid divide-y divide-slate-200 sm:grid-cols-3 sm:divide-x sm:divide-y-0">
              {PRIVACY.map((item) => (
                <div
                  key={item.title}
                  className="py-5 first:pt-0 last:pb-0 sm:px-6 sm:py-0 sm:first:pl-0 sm:last:pr-0"
                >
                  <h3 className="text-sm font-semibold text-slate-900">
                    {item.title}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-slate-600">
                    {item.body}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section aria-labelledby="access-heading">
          <div className="mx-auto max-w-6xl px-4 py-12 sm:px-6 sm:py-16">
            <div className="rounded-lg border border-amber-200 bg-amber-50/60 p-6">
              <h2 id="access-heading" className={SECTION_LABEL}>
                Access
              </h2>
              <p className="mt-3 max-w-3xl text-sm leading-6 text-slate-700">
                Dashboard access is provisioned by an administrator, not granted
                by discovery. Once your operator account is authorised, sign in
                with the credentials you were given.
              </p>
            </div>
          </div>
        </section>
      </main>

      <footer className="border-t border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-col gap-2 px-4 py-6 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between sm:px-6">
          <p className="font-mono uppercase tracking-[0.16em] text-slate-500">
            The Prawn Hunter
          </p>
          <p>
            Apache-2.0 licensed. © 2026 The Prawn Organisation. Self-hosted, with
            no third-party analytics.
          </p>
        </div>
      </footer>
    </div>
  );
}
