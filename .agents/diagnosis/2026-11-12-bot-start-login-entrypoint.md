# Diagnosis: `/start` did not enter login

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

| Field | Observation |
|---|---|
| Expected | A user opening the login bot and sending Telegram's standard `/start` command can begin the account-login flow. |
| Observed | `/start` only sends an informational response; login is registered only for `/starthunter`. |
| Scope | Every private-chat `/start` command. |
| Onset | Current command routing. |
| Repro | Send `/start` to `@telehunter234bot` in a private chat. |

## Evidence

1. The bot container is running, healthy, and has not restarted — `docker compose ps bot` and `docker inspect theprawnhunter_bot`, 2026-11-12.
2. The container's `/tmp/bot_alive` marker was five seconds old and a direct `getMe` call succeeded for `@telehunter234bot` — bot-container probes, 2026-11-12.
3. Bot logs show a successful account-login completion and invite delivery after the reported issue — bot logs at 2026-11-12 00:06 UTC.
4. `runtime.py` registers `/start` to `commands.start` and only registers `starthunter` as the `ConversationHandler` entry point — `app/services/_bot_listener/runtime.py:230-262`.
5. `commands.start` sends a short-lived instruction to use `/starthunter`; it does not invoke the login handler — `app/services/_bot_listener/commands.py:12-27`.
6. A Telegram `TimedOut` update exception occurred before recovery, but subsequent Bot API and liveness probes succeeded — bot logs and direct `getMe` probe, 2026-11-12.

## Root Cause

The bot was available. The standard `/start` command is routed to an informational handler instead of the existing guarded login conversation, and its response is auto-deleted. This makes an online bot appear non-functional to a user who expects `/start` to begin onboarding. The observed network timeout was transient and recovered; it was not the cause of the persistent command behavior.

## Fix Options

| Option | Changes | Risk | Recommendation |
|---|---|---|---|
| Document `/starthunter` only | None | Repeats the confusing default Telegram experience. | No |
| In private chats, delegate `/start` to the existing `starthunter` handler | One handler branch, existing authorization/rate-limit behavior retained | `/start` consumes a login-attempt rate-limit slot, as intended for a login start. | Yes |
| Add a new parallel conversation/button system | New UI and state paths | Duplicates existing login controls. | No |

## Verification Plan

1. Add a unit test proving private `/start` delegates to the existing login entry point.
2. Add a unit test proving non-private `/start` retains its informational behavior.
3. Run the focused bot-listener tests.
4. Rebuild only the bot service and confirm `getMe`, liveness, and polling heartbeat.

## Recurrence Guard

The invariant is: *the bot's standard private-chat entry point must reach the same authorization and login state machine as `/starthunter`*. Encode this in command-handler tests.
