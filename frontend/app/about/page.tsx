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
      <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.24em] text-emerald-400">
        {eyebrow}
      </p>
      <h2
        id={id}
        className="mt-4 text-3xl font-bold tracking-tight text-white sm:text-4xl"
      >
        {title}
      </h2>
    </div>
  );
}

export default function AboutPage() {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-300 selection:bg-emerald-500/30 selection:text-emerald-50">
      <header className="mx-auto flex max-w-6xl items-center justify-between px-6 py-6 sm:px-10">
        <div className="flex items-center gap-3">
          <Mark className="h-6 w-6 text-emerald-400" />
          <span className="font-mono text-xs font-semibold uppercase tracking-[0.22em] text-slate-200">
            The Prawn Hunter
          </span>
        </div>
        <span className="hidden items-center gap-2 font-mono text-[11px] uppercase tracking-[0.18em] text-slate-500 sm:flex">
          <span
            className="h-1.5 w-1.5 rounded-full bg-emerald-400"
            aria-hidden="true"
          />
          Operator console
        </span>
      </header>

      <main>
        <section aria-labelledby="hero-heading" className="relative overflow-hidden">
          <div
            aria-hidden="true"
            className="pointer-events-none absolute -top-40 right-0 h-96 w-96 rounded-full bg-emerald-500/10 blur-3xl"
          />
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-emerald-500/40 to-transparent"
          />
          <div className="relative mx-auto grid max-w-6xl gap-14 px-6 pb-20 pt-10 sm:px-10 sm:pb-28 sm:pt-16 lg:grid-cols-[1.05fr_0.95fr] lg:items-center">
            <div>
              <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.24em] text-emerald-400">
                Self-hosted OSINT pipeline
              </p>
              <h1
                id="hero-heading"
                className="mt-5 max-w-2xl text-4xl font-bold leading-[1.05] tracking-tight text-white sm:text-5xl lg:text-6xl"
              >
                Exposed Telegram bots, found, proven, kept private.
              </h1>
              <p className="mt-6 max-w-xl text-base leading-7 text-slate-400 sm:text-lg">
                The Prawn Hunter discovers leaked Telegram Bot API tokens across
                public sources, validates each one against the live Telegram API,
                harvests the chat history those bots can still reach, and delivers
                the evidence to a private operator supergroup and dashboard.
              </p>
              <div className="mt-9 flex flex-col gap-3 sm:flex-row sm:items-center">
                <a
                  href="/signin"
                  className="inline-flex items-center justify-center gap-2 rounded-md bg-emerald-500 px-6 py-3 text-sm font-semibold text-slate-950 transition-colors hover:bg-emerald-400 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400"
                >
                  Sign in
                  <span aria-hidden="true">→</span>
                </a>
                <a
                  href="/"
                  className="inline-flex items-center justify-center rounded-md border border-white/15 px-6 py-3 text-sm font-medium text-slate-200 transition-colors hover:border-white/30 hover:bg-white/5 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-emerald-400"
                >
                  Back to the app
                </a>
              </div>
              <p className="mt-6 font-mono text-[11px] uppercase tracking-[0.16em] text-slate-500">
                Operator access only
              </p>
            </div>

            <div className="rounded-2xl border border-white/10 bg-slate-900/60 p-6 shadow-2xl shadow-black/40 backdrop-blur">
              <div className="flex items-center justify-between border-b border-white/10 pb-4">
                <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-slate-400">
                  Pipeline
                </span>
                <span className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.16em] text-emerald-400">
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-emerald-400"
                    aria-hidden="true"
                  />
                  Self-hosted
                </span>
              </div>
              <ul className="divide-y divide-white/5">
                {PIPELINE.map((step) => (
                  <li
                    key={step.index}
                    className="flex items-start justify-between gap-4 py-4"
                  >
                    <div className="flex items-start gap-4">
                      <span className="font-mono text-[11px] text-slate-500">
                        {step.index}
                      </span>
                      <div>
                        <p className="text-sm font-semibold text-slate-100">
                          {step.title}
                        </p>
                        <p className="mt-1 font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
                          {step.detail}
                        </p>
                      </div>
                    </div>
                    <span
                      className="mt-1 font-mono text-xs text-emerald-500"
                      aria-hidden="true"
                    >
                      →
                    </span>
                  </li>
                ))}
              </ul>
              <div className="mt-2 flex items-center justify-between border-t border-white/10 pt-4 font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500">
                <span>Evidence destination</span>
                <span className="text-emerald-400">Private</span>
              </div>
            </div>
          </div>
        </section>

        <section
          aria-labelledby="steps-heading"
          className="border-y border-white/10 bg-slate-900/30"
        >
          <div className="mx-auto max-w-6xl px-6 py-20 sm:px-10 sm:py-24">
            <SectionHeading
              id="steps-heading"
              eyebrow="How it works"
              title="Four stages, one private pipeline."
            />
            <ol className="mt-10 grid overflow-hidden rounded-2xl border border-white/10 bg-white/10 sm:grid-cols-2 lg:grid-cols-4">
              {PIPELINE.map((step) => (
                <li key={step.index} className="bg-slate-950 p-6 sm:p-7">
                  <span className="font-mono text-xs text-emerald-400">
                    {step.index}
                  </span>
                  <h3 className="mt-4 text-lg font-semibold text-white">
                    {step.title}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-slate-400">
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
          className="mx-auto max-w-6xl px-6 py-20 sm:px-10 sm:py-24"
        >
          <SectionHeading
            id="features-heading"
            eyebrow="Operator surfaces"
            title="Built for an operator, not a crowd."
          />
          <div className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((feature) => (
              <article
                key={feature.title}
                className="rounded-xl border border-white/10 bg-slate-900/40 p-6 transition-colors hover:border-emerald-500/40 hover:bg-slate-900/70"
              >
                <h3 className="text-base font-semibold text-white">
                  {feature.title}
                </h3>
                <p className="mt-3 text-sm leading-6 text-slate-400">
                  {feature.body}
                </p>
              </article>
            ))}
          </div>
        </section>

        <section
          aria-labelledby="privacy-heading"
          className="border-y border-white/10 bg-slate-900/30"
        >
          <div className="mx-auto max-w-6xl px-6 py-20 sm:px-10">
            <h2
              id="privacy-heading"
              className="font-mono text-[11px] font-semibold uppercase tracking-[0.24em] text-emerald-400"
            >
              Private by design
            </h2>
            <div className="mt-8 grid gap-8 sm:grid-cols-3 sm:gap-6">
              {PRIVACY.map((item) => (
                <div key={item.title}>
                  <h3 className="text-base font-semibold text-white">
                    {item.title}
                  </h3>
                  <p className="mt-2 text-sm leading-6 text-slate-400">
                    {item.body}
                  </p>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="mx-auto max-w-6xl px-6 py-20 sm:px-10 sm:py-24">
          <div className="rounded-2xl border border-emerald-500/30 bg-emerald-500/5 p-8 sm:p-10">
            <p className="font-mono text-[11px] font-semibold uppercase tracking-[0.24em] text-emerald-400">
              Access
            </p>
            <p className="mt-4 max-w-2xl text-xl leading-8 text-slate-200 sm:text-2xl">
              Dashboard access is provisioned by an administrator, not granted by
              discovery. Once your operator account is authorised, sign in with the
              credentials you were given.
            </p>
          </div>
        </section>
      </main>

      <footer className="border-t border-white/10">
        <div className="mx-auto flex max-w-6xl flex-col gap-3 px-6 py-8 text-xs text-slate-500 sm:flex-row sm:items-center sm:justify-between sm:px-10">
          <p className="font-mono uppercase tracking-[0.16em] text-slate-400">
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
