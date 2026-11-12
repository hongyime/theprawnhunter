# Diagnosis: login command re-entry bypasses the conversation state machine

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

| Field | Evidence |
| --- | --- |
| Expected | `/start` and `/starthunter` in a private chat should begin or safely restart the login flow. Re-entering during phone/code/2FA entry must leave one clean login attempt. |
| Observed | Commit `dac4620` makes private `/start` directly call `login.starthunter`, while the registered `ConversationHandler` accepts only `/starthunter`. |
| Scope | Any private `/start`; especially a command issued while the handler is in `WAIT_CODE` or `WAIT_PASSWORD`. |
| Onset | `dac4620 fix(bot): start the login flow from private start` (current `HEAD`). |
| Repro | Start `/starthunter`, submit a phone number, then issue `/start`. |

## Evidence

1. `app/services/_bot_listener/runtime.py` registers `CommandHandler("start", start)` separately, and the `ConversationHandler` only has `CommandHandler('starthunter', starthunter)` as an entry point.
2. `app/services/_bot_listener/commands.py:start` calls `starthunter` directly for private chats. A standalone `CommandHandler` does not update `ConversationHandler` state.
3. `login.starthunter` returns `WAIT_PHONE`; `telegram.ext.ConversationHandler.handle_update` is the component that writes a callback's returned state, as confirmed from the installed library source.
4. `login.handle_phone` stores a connected client and temporary session in `context.user_data`; `starthunter` previously did not clean either on re-entry.

## Root Cause

The `/start` command dispatches the login callback outside the `ConversationHandler`. Its return value cannot change the active conversation state, and it does not clean the prior temporary client/session. A command during code or 2FA entry can therefore show a new phone prompt while the handler still consumes the next text as a code or password.

## Why It Was Not Obvious

The direct callback test in `tests/unit/test_bot_start_login.py` verified the delegation but not handler registration or conversation-state ownership.

## Fix Options

| Option | Changes | Risk | Recommendation |
| --- | --- | --- | --- |
| Register both commands as conversation entry points with re-entry enabled, and clean an active attempt before prompting again | Runtime, login handler, focused tests | A re-entry intentionally abandons the prior code request | Recommended |
| Keep separate `/start` handler and manually mutate conversation state | Relies on private framework internals | Fragile across library updates | Reject |

## Verification Plan

1. Assert the runtime registers `/start` and `/starthunter` on the same `ConversationHandler` with `allow_reentry=True`.
2. Assert a re-entry disconnects and removes the temporary session before yielding `WAIT_PHONE`.
3. Run focused login and runtime tests.

## Recurrence Guard

Keep a registration-level test: every command that returns a login state must be an entry point of the owning `ConversationHandler`.
