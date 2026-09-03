# Diagnosis: Supabase Auth transactional email bounces

**Date:** 2026-09-02  
**Project:** `finjklyfedduvtzqjqad`  
**Status:** SUSPECTED — the sending events are identified, but the bounce recipients and request origin require Supabase/provider logs

## Symptom Contract

- **Expected:** Supabase Auth sends transactional email only for intentional production workflows and to valid, controlled recipients, with a low hard-bounce rate.
- **Observed:** Supabase warned that project `finjklyfedduvtzqjqad` has a high transactional-email bounce rate and may have sending restricted.
- **Scope:** Signup-confirmation email is the only email-triggering Auth action visible in the current production user records. No password-recovery or invite timestamps are present.
- **Onset:** Three confirmation messages were triggered on 2026-08-31: two at 16:45 UTC and one at 18:32 UTC. The warning was received on 2026-09-02.
- **Reproduction:** Not intentionally reproduced because another invalid delivery could worsen the bounce rate. Use provider sandbox addresses or local Mailpit for future tests.

## Evidence

### Repository inspection

- A full source search found no calls to `supabase.auth`, `signUp`, `signInWithOtp`, `resetPasswordForEmail`, `inviteUserByEmail`, or `generateLink`.
- `frontend/lib/supabase.ts` creates a Supabase client, but the checked-in frontend uses it for data access/realtime rather than Auth email flows.
- The linked project reference in `supabase/.temp/project-ref` is `finjklyfedduvtzqjqad`, matching the warning.
- Local `supabase/config.toml` enables general and email signup, uses `max_frequency = "1s"`, and leaves CAPTCHA disabled. This is a local configuration file and does not prove those exact values are deployed.

### Read-only production Auth checks

- Public Auth settings report that signup is enabled, email is enabled, and email auto-confirm is disabled. Therefore a public email signup can trigger a confirmation message.
- There are only 3 Auth users, all using the email provider and all created within the two August 31 time windows above.
- All 3 records have a `confirmation_sent_at` timestamp; 0 have invite or recovery-send timestamps.
- Only 1 of 3 users has confirmed and signed in. The other 2 remain unconfirmed.
- All 3 addresses pass basic syntax checks and their two unique domains publish MX records. Syntax/MX validation alone would not have prevented the incident.
- No addresses, tokens, or service credentials were printed during these checks.

### Current Supabase behavior

- Supabase documents that the default SMTP service is best-effort, heavily limited, and not intended for production. Production Auth email should use custom SMTP or a Send Email Auth Hook.
- Auth endpoints that trigger email include `/auth/v1/signup`, `/auth/v1/recover`, `/auth/v1/user`, and `/auth/v1/otp`; project-wide and per-recipient rate limits apply.
- Supabase supports CAPTCHA on signup, sign-in, and password-reset flows.
- Auth and edge logs are the supported source for request path, timestamp, IP, user agent, referrer, response status, and mailer errors.

## Root Cause

### Confirmed proximate cause

Three email-provider signups triggered three confirmation emails on August 31. With such a small denominator, two hard bounces would produce a 66.7% bounce rate, which is consistent with a high-bounce warning. The two unconfirmed records are candidates, but lack of confirmation is not proof of a bounce.

### Suspected underlying cause

The confirmation requests did not originate from the code in this repository. They most likely came from one of:

1. Manual/test signups against the production project.
2. A separate app or old deployment sharing this Supabase project and publishable key.
3. Direct/bot requests to the public Auth signup endpoint while email signup is enabled.

The exact branch cannot be confirmed without the Auth/edge log rows or the SMTP provider's bounce events.

## Why This Was Not Obvious

- The application can use Supabase database/realtime features without using Supabase Auth, while Auth remains independently enabled at the project level.
- The local Auth configuration looks like a normal Supabase CLI default and can be mistaken for the live dashboard configuration.
- Valid syntax and a domain with MX records do not prove that a mailbox exists.
- An unconfirmed user may reflect a bounce, ignored mail, spam placement, or abandonment.
- At a volume of only three messages, a small number of bad test addresses creates an extreme percentage.

## Fix Options

| Option | Value | Cost / risk |
|---|---|---|
| Disable email signup/provider if this product does not use Supabase Auth | Immediately stops public signup confirmation sends | Existing email Auth flows stop; verify that no other app shares the project first |
| Keep Auth but restrict the legitimate entry point and enable CAPTCHA | Reduces automated/direct abuse | Requires the real Auth client to pass CAPTCHA tokens |
| Raise the per-recipient resend window from 1 second to at least the current Supabase default | Prevents repeat confirmation bursts to one recipient | Local config must be reconciled with live dashboard settings |
| Configure a dedicated custom SMTP provider and verified Auth subdomain | Adds bounce/suppression visibility and improves deliverability | Does not make invalid mailboxes valid; domain authentication and provider monitoring are required |
| Add a Send Email Auth Hook with suppression/queue logic | Maximum control over bounces, retries, and routing | More moving parts than this three-user project currently justifies |
| Use local Mailpit/provider sandbox inboxes for development | Prevents production test bounces | Requires separating local/staging test practice from production |

## Recommended Containment Order

1. In Supabase Logs Explorer, select 2026-08-31 16:30–19:00 UTC and inspect Auth plus edge logs for `/auth/v1/signup`, mailer errors, IP, user agent, and referrer.
2. If Supabase Auth is unused by every app attached to this project, turn off **Allow new users to sign up** and/or the Email provider now.
3. If Auth is used, enable Turnstile or hCaptcha, restore a conservative per-recipient resend interval, and keep email-send limits low while investigating.
4. Stop sending to the two unconfirmed addresses. Do not retry them unless the owners correct or reconfirm the addresses through a trusted channel.
5. Before re-enabling normal production volume, configure custom SMTP with SPF, DKIM, and DMARC, plus provider bounce/complaint suppression.
6. Reply to Supabase Support with the three-message scope, the containment performed, and a request for the bounced recipient hashes/timestamps if those details are not visible in project logs.

## Verification Plan

- Auth/edge logs identify the request origin for all three August 31 signup events.
- No unintended `/auth/v1/signup`, `/auth/v1/recover`, or `/auth/v1/otp` email sends occur for 7 days.
- Any enabled production Auth flow succeeds to a single known-good inbox using the intended UI and CAPTCHA.
- SMTP provider dashboards show SPF/DKIM/DMARC passing, bounce suppression enabled, and zero hard bounces during the observation window.
- Development email tests appear only in Mailpit or the provider's sandbox, never in production delivery metrics.

## Recurrence Guard

- Keep email signup disabled unless a product flow explicitly needs it.
- Add Auth/edge log monitoring for unexpected signup spikes and 4xx/5xx mailer responses.
- Maintain a hard-bounce suppression list at the email provider and never automatically retry permanent failures.
- Use a separate Auth sending subdomain/address from marketing mail.
- Document which deployed clients are authorized to use this Supabase project.
- Reconcile `supabase/config.toml` with production Auth settings before linking/pushing config changes.

