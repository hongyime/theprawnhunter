# Diagnosis: successful logins leave old bot and service-chat history

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

| Field | Evidence |
| --- | --- |
| Expected | Sixty seconds after a successful login, clear the full history of the account's DM with TeleHunter and its Telegram service-notification chat, including old messages. |
| Observed | Residual messages remain. |
| Scope | Successful account login only. |
| Repro | Complete a login and inspect the account's two dialogs after the bot's normal message TTL elapses. |

## Evidence

1. `common.py:_reply_and_delete` deletes only the individual bot response after 30 seconds; it cannot delete user messages or old dialog history.
2. `login.py:finalize_login` inspects only the latest 15 service messages and deletes only messages containing selected keywords; it does not clear the dialog.
3. The same method calls `client.delete_dialog(bot_entity)` with Telethon's default `revoke=False`.
4. Installed Telethon source documents `delete_dialog(..., revoke=True)` as the option that deletes private-chat history for both peers. It also confirms the method requires an input entity; the current numeric-ID fallback has already logged an input-entity resolution error.

## Root Cause

The cleanup is immediate, partial, and uses a dialog-delete call that neither resolves the bot entity reliably nor requests history revocation. It therefore cannot meet a whole-dialog, all-ages cleanup requirement.

## Fix Options

| Option | Changes | Risk | Recommendation |
| --- | --- | --- | --- |
| After a saved session, schedule a 60-second user-account cleanup that deletes both dialogs with `revoke=True`, resolving the bot by username | Login flow and unit test | A bot process restart in the 60-second window cancels the in-memory task | Recommended smallest change |
| Persist a cleanup job in the worker queue | Schema/worker/deploy | More moving parts | Upgrade if cleanup must survive bot restarts |

## Verification Plan

1. Unit-test that the scheduled cleanup waits, uses `revoke=True` for both dialogs, and always disconnects.
2. Run focused login tests.
3. Confirm a new successful login clears both dialogs after 60 seconds.

## Recurrence Guard

Test dialog cleanup with the explicit `revoke=True` contract rather than relying on Telethon defaults.
