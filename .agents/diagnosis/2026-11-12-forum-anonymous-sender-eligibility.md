# Diagnosis: forum anonymous-admin senders are incorrectly excluded

Date: 2026-11-12 | Repo: `/home/bryan/Projects/theprawnhunter` | Status: CONFIRMED

## Symptom Contract

| Field | Evidence |
| --- | --- |
| Expected | Every authorized anonymous admin in the monitor forum should be available to anonymous-session rotation. |
| Observed | The eligibility probe requires `post_messages`; a read-only live probe found five authorized anonymous admins, but only four have that flag. |
| Scope | Forum/supergroup monitor chats. |
| Repro | Call `eligible_anonymous_senders` for a forum where an anonymous admin has `post_messages=False`. |

## Evidence

1. `group_archive.py:eligible_anonymous_senders` requires `admin_rights.anonymous and admin_rights.post_messages` for every channel type.
2. The monitor chat is a forum supergroup (`broadcast=False`, observed in the prior direct participant probe), not a broadcast channel.
3. Live read-only probe on 2026-11-12 found `{total: 6, unauthorized: 1, admin: 5, anonymous: 5, post_messages: 4, manage_topics: 4}`.
4. Telegram's `post_messages` administrator permission is for broadcast channels; forum supergroups use normal member/admin message permissions. The current code therefore turns one valid anonymous forum sender into a false negative.

## Root Cause

Eligibility applies a broadcast-channel-specific permission to all channel entities instead of branching on the resolved chat's `broadcast` flag.

## Fix Options

| Option | Changes | Risk | Recommendation |
| --- | --- | --- | --- |
| Require anonymous for forums/supergroups; require anonymous plus post permission for broadcast channels | Eligibility predicate and focused tests | A Telegram-side send can still fail and remains logged/retried | Recommended |
| Require `post_messages` everywhere | None | Leaves valid forum senders unused | Reject |

## Verification Plan

1. Unit-test a forum admin with anonymous true/post false is included.
2. Unit-test a broadcast admin with anonymous true/post false is excluded.
3. Clear the five-minute eligibility cache and observe the worker discover five candidates.

## Recurrence Guard

Keep entity-type-specific permission cases in the eligibility test suite.
