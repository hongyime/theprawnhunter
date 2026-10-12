"""Diagnostics flow task definitions."""

from .common import *

@app.task(name="flow.unpin_all_webhook_messages")
def unpin_all_webhook_messages(max_credentials: int = 500):
    """Sweep every credential with pinned_webhook_msg_id, unpin the Telegram
    message, and clear the meta field. One-shot cleanup — safe to re-run."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_unpin_all_webhook_messages_logic(max_credentials))

async def _unpin_all_webhook_messages_logic(max_credentials: int) -> dict:
    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, meta")
            .not_.is_("meta->>pinned_webhook_msg_id", "null")
            .limit(max_credentials)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []
    if not rows:
        return {"status": "no_pinned_messages"}

    broadcaster = get_broadcaster()
    unpinned = 0
    failed = 0

    for row in rows:
        meta = row.get("meta") or {}
        pinned_id = meta.get("pinned_webhook_msg_id")
        if not pinned_id:
            continue
        try:
            ok = await broadcaster.unpin_message(settings.MONITOR_GROUP_ID, int(pinned_id))
            if ok:
                unpinned += 1
            else:
                failed += 1
        except Exception as e:
            logger.debug(f"[UnpinAll] unpin failed for msg {pinned_id}: {e}")
            failed += 1

        # Clear the meta field regardless — the pinned message may already
        # be deleted/inaccessible, we still want the record cleaned up.
        try:
            new_meta = {k: v for k, v in meta.items() if k not in (
                "pinned_webhook_msg_id", "pinned_webhook_at"
            )}
            await async_execute(
                db.table("discovered_credentials")
                .update({"meta": new_meta})
                .eq("id", row["id"])
            )
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

    msg = f"📌 Unpinned {unpinned} webhook messages ({failed} unpin failures)"
    logger.info(msg)
    with contextlib.suppress(Exception):
        await broadcaster.send_log(msg)

    return {"status": "ok", "unpinned": unpinned, "failed": failed, "total": len(rows)}

@app.task(name="flow.webhook_port_sitemap_diag")
def webhook_port_sitemap_diag():
    """Diagnostic report: port distribution + reachable-path stats + sitemap
    findings across all captured webhook_probe results."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_webhook_port_sitemap_diag_logic())

async def _webhook_port_sitemap_diag_logic() -> dict:
    from collections import Counter
    from urllib.parse import urlparse

    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, meta")
            .not_.is_("meta->>webhook_url", "null")
            .limit(2000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    port_counter: Counter = Counter()
    scheme_counter: Counter = Counter()
    reachable_path_counter: Counter = Counter()
    sitemap_hits: list[dict] = []
    leaks: list[dict] = []
    total_with_probe = 0

    for row in res.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue

        # Extract port from URL (defaults 443/80 by scheme)
        try:
            parsed = urlparse(url)
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        port_counter[port] += 1
        scheme_counter[parsed.scheme or "unknown"] += 1

        probe = meta.get("webhook_probe") or {}
        if probe:
            total_with_probe += 1

        # Reachable paths (status 200)
        web_recon = probe.get("web_recon") or {}
        for path, info in web_recon.items():
            if not isinstance(info, dict):
                continue
            if info.get("status") == 200:
                reachable_path_counter[path] += 1
            # Sitemap capture
            if path == "/sitemap.xml" and info.get("status") == 200:
                sample = (info.get("preview") or "")[:200]
                sitemap_hits.append(
                    {"host": parsed.hostname, "url": url, "preview": sample}
                )
            # Sensitive-file leak
            if path in ("/.env", "/.git/config", "/server-status") and info.get("leak_preview"):
                leaks.append(
                    {"host": parsed.hostname, "path": path, "preview": info["leak_preview"][:150]}
                )

    lines = [
        "🔎 **Webhook Port + Sitemap Diagnostic**",
        f"Analyzed {len(res.data or [])} webhook URLs ({total_with_probe} with probe data)",
        "",
        "**Port distribution:**",
    ]
    for port, count in port_counter.most_common(10):
        pct = count / max(len(res.data or []), 1) * 100
        lines.append(f"• `{port}` × {count} ({pct:.0f}%)")

    lines.append("")
    lines.append("**Scheme distribution:**")
    for scheme, count in scheme_counter.most_common():
        lines.append(f"• `{scheme}` × {count}")

    lines.append("")
    lines.append("**Top reachable paths (HTTP 200):**")
    for path, count in reachable_path_counter.most_common(10):
        lines.append(f"• `{path}` × {count}")

    if sitemap_hits:
        lines.append("")
        lines.append(f"**Sitemap.xml hits: {len(sitemap_hits)} hosts**")
        for hit in sitemap_hits[:5]:
            lines.append(f"• `{hit['host']}`")

    if leaks:
        lines.append("")
        lines.append(f"**Sensitive-file leaks: {len(leaks)}**")
        for leak in leaks[:5]:
            lines.append(f"• `{leak['host']}` → `{leak['path']}`")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "urls_analyzed": len(res.data or []),
        "urls_with_probe": total_with_probe,
        "top_port": port_counter.most_common(1)[0] if port_counter else None,
        "sitemap_hosts": len(sitemap_hits),
        "sensitive_leaks": len(leaks),
    }

@app.task(name="flow.message_flow_diag")
def message_flow_diag():
    """Diagnose message flow health — recent ingest rate, takeover-vs-capture
    correlation, per-bot activity distribution."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_message_flow_diag_logic())

async def _message_flow_diag_logic() -> dict:
    from collections import Counter
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    windows = [
        ("last 1h", now - timedelta(hours=1)),
        ("last 24h", now - timedelta(hours=24)),
        ("last 7d", now - timedelta(days=7)),
    ]

    ingest_by_window: dict[str, int] = {}
    for label, since in windows:
        try:
            r = await async_execute(
                db.table("exfiltrated_messages")
                .select("id", count="exact")
                .gte("created_at", since.isoformat())
                .limit(1)
            )
            ingest_by_window[label] = r.count or 0
        except Exception:
            ingest_by_window[label] = -1

    # Per-bot ingest (last 24h) — which credentials are actually producing?
    since_24h = (now - timedelta(hours=24)).isoformat()
    try:
        recent = await async_execute(
            db.table("exfiltrated_messages")
            .select("credential_id")
            .gte("created_at", since_24h)
            .limit(5000)
        )
        bot_activity = Counter(r.get("credential_id") for r in (recent.data or []) if r.get("credential_id"))
    except Exception:
        bot_activity = Counter()

    # Active credentials with recent activity
    active_with_ingest_24h = len(bot_activity)

    # Total active credentials
    try:
        total_active = await async_execute(
            db.table("discovered_credentials")
            .select("id", count="exact")
            .eq("status", "active")
            .limit(1)
        )
        total_active_count = total_active.count or 0
    except Exception:
        total_active_count = 0

    activity_pct = (active_with_ingest_24h / total_active_count * 100) if total_active_count else 0.0

    # Recent takeovers — used to correlate takeover rate vs message rate
    try:
        takeover_24h = await async_execute(
            db.table("audit_logs")
            .select("id", count="exact")
            .eq("event_type", "webhook.takeover")
            .gte("created_at", since_24h)
            .limit(1)
        )
        takeover_count = takeover_24h.count or 0
    except Exception:
        takeover_count = -1

    lines = [
        "📉 **Message Flow Diagnostic**",
        "",
        "**Ingest volume:**",
    ]
    for label, count in ingest_by_window.items():
        lines.append(f"• {label}: {count:,} messages")

    lines.append("")
    lines.append("**Bot activity (last 24h):**")
    lines.append(
        f"• {active_with_ingest_24h} of {total_active_count} active bots produced ≥1 msg "
        f"({activity_pct:.1f}%)"
    )
    lines.append(f"• {takeover_count} webhook takeovers succeeded")

    if bot_activity:
        lines.append("")
        lines.append("**Top 5 message-producing bots (24h):**")
        for cred_id, count in bot_activity.most_common(5):
            lines.append(f"• `{cred_id[:8]}...` × {count} msgs")

    lines.append("")
    lines.append("**Diagnosis:**")
    if ingest_by_window.get("last 24h", 0) < 100:
        lines.append(
            "⚠️  Low 24h volume. Likely causes:\n"
            "   1. Most bots have no real users interacting with them (dead bots)\n"
            "   2. Third parties re-register webhooks faster than we takeover\n"
            "   3. getUpdates polling cadence — check rescrape frequency\n"
            "   4. Bots may be rate-limited by Telegram after mass takeover"
        )
    else:
        lines.append("✅ Flow appears healthy.")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "ingest_by_window": ingest_by_window,
        "active_with_ingest_24h": active_with_ingest_24h,
        "total_active": total_active_count,
        "takeover_count_24h": takeover_count,
    }

@app.task(name="flow.reconcile_topics_from_db")
def reconcile_topics_from_db(max_credentials: int = 500):
    """Reconciliation: Supabase is source of truth. For each active credential,
    verify topic exists and trigger re-broadcast of any messages that were
    inserted but not yet delivered (is_broadcasted=False).

    Does NOT reset already-broadcasted messages to False — that would flood
    topics with duplicates. Purely fills gaps forward.

    Broadcasts a summary of what was reconciled.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _reconcile_topics_from_db_logic(max_credentials)
    )

async def _reconcile_topics_from_db_logic(max_credentials: int) -> dict:
    # 1. Count pending (is_broadcasted=False) messages per credential
    try:
        pending = await async_execute(
            db.table("exfiltrated_messages")
            .select("credential_id")
            .eq("is_broadcasted", False)
            .limit(10000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    from collections import Counter

    pending_by_cred = Counter(
        r.get("credential_id") for r in (pending.data or []) if r.get("credential_id")
    )

    total_pending = sum(pending_by_cred.values())
    if total_pending == 0:
        msg = (
            "🔄 **Topic Reconciliation** — DB clean. "
            "0 messages pending broadcast across all credentials."
        )
        with contextlib.suppress(Exception):
            await get_broadcaster().send_log(msg)
        return {"status": "clean", "pending": 0}

    # 2. Which credentials need topic verification? Look at ones with pending msgs.
    cred_ids = list(pending_by_cred.keys())[:max_credentials]
    try:
        creds = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_username, chat_name, meta")
            .in_("id", cred_ids)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    missing_topic_count = 0
    for row in creds.data or []:
        meta = row.get("meta") or {}
        if not meta.get("topic_id"):
            missing_topic_count += 1

    # 3. Trigger the broadcast_pending task to churn through pending queue.
    #    Broadcaster is the ONLY code path that should mark is_broadcasted=True
    #    so this is safe (no duplication).
    try:
        app.send_task("flow.broadcast_pending")
        dispatched = True
    except Exception:
        dispatched = False

    top_backlog = pending_by_cred.most_common(10)

    lines = [
        "🔄 **Topic Reconciliation (Supabase = source of truth)**",
        "",
        f"• Total pending broadcasts: {total_pending}",
        f"• Credentials affected: {len(pending_by_cred)}",
        f"• Credentials missing topic_id: {missing_topic_count}",
        f"• broadcast_pending dispatched: {dispatched}",
        "",
        "**Top backlog (credential → pending count):**",
    ]
    for cred_id, count in top_backlog:
        lines.append(f"• `{cred_id[:8]}...` × {count}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "total_pending": total_pending,
        "credentials_affected": len(pending_by_cred),
        "credentials_missing_topic": missing_topic_count,
        "broadcast_dispatched": dispatched,
    }

@app.task(name="flow.pin_general_readme")
def pin_general_readme():
    """One-shot: post a README to the General topic and pin it (chat-level pin
    so it's visible to anyone stumbling into the group). Safe to re-run —
    replaces existing pinned readme by unpinning previous first."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_pin_general_readme_logic())

async def _pin_general_readme_logic() -> dict:
    from datetime import datetime

    webapp_url = "https://theprawnhunter.hong-yi.me"
    readme = (
        "🦐 **Prawn Hunter — Monitor Group**\n"
        "\n"
        "This group receives *passive OSINT observations* on Telegram bots whose "
        "tokens have been exposed publicly (GitHub, extension scrapes, etc). "
        "Each topic below is one compromised bot; messages you see are what real "
        "users are sending to those bots — captured after we take over their "
        "third-party webhook.\n"
        "\n"
        "**What we do**\n"
        "• Scan 13+ public data sources for leaked bot tokens\n"
        "• Passively fingerprint any third-party C2 (TLS, Shodan, web recon)\n"
        "• Delete third-party webhooks and poll updates ourselves\n"
        "• Broadcast captured messages to per-bot topics here\n"
        "\n"
        "**What we don't do**\n"
        "• Message users on those bots\n"
        "• Modify or scam anyone\n"
        "• Reveal your identity — all telemetry is server-side\n"
        "\n"
        f"**Live dashboard**: {webapp_url}\n"
        "(mobile-friendly; anon read-only view of Discovered Bots + Telemetry)\n"
        "\n"
        "**Admin commands** (whitelisted users only):\n"
        "`/status` `/pause` `/resume` `/bots` `/starthunter` `/help`\n"
        "\n"
        f"_Last updated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}_"
    )

    broadcaster = get_broadcaster()

    # Unpin previous readme (best-effort)
    try:
        prev = await async_execute(
            db.table("system_state")
            .select("value")
            .eq("key", "pinned_readme_msg_id")
            .limit(1)
        )
        if prev.data:
            prev_id = int(prev.data[0]["value"])
            with contextlib.suppress(Exception):
                await broadcaster.unpin_message(settings.MONITOR_GROUP_ID, prev_id)
    except Exception as _swallowed:
        # system_state table might not exist yet — that's fine
        logger.debug(f"[suppressed] {_swallowed}")
        pass

    # Send new readme to General topic (no thread_id → chat-level = General)
    try:
        bot = broadcaster._get_bot_instance(broadcaster.bot_tokens[0])
        sent = await bot.send_message(
            chat_id=settings.MONITOR_GROUP_ID,
            text=readme,
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        sent_id = sent.message_id
    except Exception as e:
        return {"status": "send_failed", "error": str(e)[:200]}

    # Pin chat-wide (in General/main chat)
    try:
        pinned = await broadcaster.pin_message(settings.MONITOR_GROUP_ID, sent_id)
    except Exception as e:
        return {"status": "pin_failed", "message_id": sent_id, "error": str(e)[:200]}

    # Track the pinned id so re-runs unpin previous
    try:
        await async_execute(
            db.table("system_state").upsert(
                {"key": "pinned_readme_msg_id", "value": str(sent_id)},
                on_conflict="key",
            )
        )
    except Exception as _swallowed:
        # system_state may not exist — pin still works, just no dedup
        logger.debug(f"[suppressed] {_swallowed}")
        pass

    return {"status": "ok", "message_id": sent_id, "pinned": pinned}

@app.task(name="flow.audit_user_agent_group_membership")
def audit_user_agent_group_membership():
    """Verify every active session's account is still a member of the monitor
    group with proper admin permissions. Auto-fixes:
      • Promotes members who aren't yet admin-promoted (catches ChatMemberHandler
        missed events after bot restarts)
      • Marks accounts as inactive when they've left the group
    Broadcasts summary to monitor group.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _audit_user_agent_group_membership_logic()
    )

async def _audit_user_agent_group_membership_logic() -> dict:
    import httpx

    monitor_bot_token = None
    tokens = (settings.MONITOR_BOT_TOKEN or "").split(",")
    if tokens and tokens[0].strip():
        monitor_bot_token = tokens[0].strip()
    if not monitor_bot_token:
        return {"status": "no_bot_token"}

    group_id = settings.MONITOR_GROUP_ID
    if not group_id:
        return {"status": "no_group_id"}

    try:
        res = await async_execute(
            db.table("telegram_accounts")
            .select("id, phone, telegram_user_id, is_admin_promoted")
            .eq("status", "active")
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    accounts = res.data or []
    if not accounts:
        return {"status": "no_accounts"}

    base_url = f"https://api.telegram.org/bot{monitor_bot_token}"
    in_group = 0
    not_in_group = 0
    promoted_now = 0
    already_promoted = 0
    marked_inactive: list[str] = []
    missing_user_id: list[str] = []

    from datetime import datetime

    async with httpx.AsyncClient(timeout=15.0) as client:
        for acct in accounts:
            user_id = acct.get("telegram_user_id")
            if not user_id:
                missing_user_id.append(acct["phone"])
                continue

            try:
                r = await client.get(
                    f"{base_url}/getChatMember",
                    params={"chat_id": group_id, "user_id": user_id},
                )
                if r.status_code == 200 and (r.json() or {}).get("ok"):
                    member_status = r.json()["result"]["status"]
                    # Statuses: creator, administrator, member, restricted, left, kicked
                    if member_status in ("left", "kicked"):
                        not_in_group += 1
                        # Mark inactive so pool stops trying to use it
                        try:
                            await async_execute(
                                db.table("telegram_accounts")
                                .update({
                                    "status": "inactive",
                                    "in_monitor_group": False,
                                    "last_membership_check_at": datetime.now(UTC).isoformat(),
                                })
                                .eq("id", acct["id"])
                            )
                            marked_inactive.append(acct["phone"])
                        except Exception as _swallowed:
                            logger.debug(f"[suppressed] {_swallowed}")
                        continue

                    in_group += 1

                    # SAFETY: never touch creator/administrator — preserves the 4 legacy owner
                    # accounts and any human you manually promoted with wider rights.
                    if member_status in ("creator", "administrator"):
                        already_promoted += 1
                        with contextlib.suppress(Exception):
                            await async_execute(
                                db.table("telegram_accounts")
                                .update({
                                    "is_admin_promoted": True,
                                    "in_monitor_group": True,
                                    "last_membership_check_at": datetime.now(UTC).isoformat(),
                                })
                                .eq("id", acct["id"])
                            )
                        continue

                    # If in group but not promoted, promote now
                    if member_status == "member" and not acct.get("is_admin_promoted"):
                        promote_resp = await client.post(
                            f"{base_url}/promoteChatMember",
                            data={
                                "chat_id": group_id,
                                "user_id": user_id,
                                "can_manage_chat": "false",
                                "can_delete_messages": "false",
                                "can_manage_video_chats": "false",
                                "can_restrict_members": "false",
                                "can_promote_members": "false",
                                "can_change_info": "false",
                                "can_invite_users": "true",
                                "can_pin_messages": "false",
                                "is_anonymous": "false",
                            },
                        )
                        if promote_resp.status_code == 200 and (promote_resp.json() or {}).get("ok"):
                            promoted_now += 1
                            with contextlib.suppress(Exception):
                                await async_execute(
                                    db.table("telegram_accounts")
                                    .update({
                                        "is_admin_promoted": True,
                                        "promoted_at": datetime.now(UTC).isoformat(),
                                        "in_monitor_group": True,
                                        "last_membership_check_at": datetime.now(UTC).isoformat(),
                                    })
                                    .eq("id", acct["id"])
                                )
                    else:
                        already_promoted += 1
                        with contextlib.suppress(Exception):
                            await async_execute(
                                db.table("telegram_accounts")
                                .update({
                                    "in_monitor_group": True,
                                    "last_membership_check_at": datetime.now(UTC).isoformat(),
                                })
                                .eq("id", acct["id"])
                            )
                else:
                    logger.debug(
                        "[MembershipAudit] getChatMember returned HTTP %s for "
                        "an account record",
                        r.status_code,
                    )
            except Exception as e:
                logger.debug(
                    "[MembershipAudit] check failed for an account record: %s",
                    type(e).__name__,
                )
                continue

    lines = [
        "👥 **User-Agent Group Membership Audit**",
        "",
        f"• Active accounts checked: {len(accounts)}",
        f"• In group + already promoted: {already_promoted}",
        f"• In group but needed promotion (fixed): {promoted_now}",
        f"• Not in group (marked inactive): {not_in_group}",
        f"• Missing telegram_user_id (skipped): {len(missing_user_id)}",
    ]
    if marked_inactive:
        lines.append("")
        lines.append("Marked inactive:")
        for p in marked_inactive[:5]:
            lines.append(f"  • {p}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "checked": len(accounts),
        "in_group": in_group,
        "not_in_group": not_in_group,
        "promoted_now": promoted_now,
        "already_promoted": already_promoted,
        "missing_user_id": len(missing_user_id),
    }
