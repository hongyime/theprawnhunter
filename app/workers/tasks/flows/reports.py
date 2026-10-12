"""Reports flow task definitions."""

from .common import *

@app.task(name="flow.report_pin_metrics")
def report_pin_metrics():
    """Broadcast a summary of webhook takeover activity — pins performed,
    webhook URLs captured, and top C2 hosts. Scheduled every 12h."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_report_pin_metrics_logic())

async def _report_pin_metrics_logic() -> dict:
    from collections import Counter
    from urllib.parse import urlparse

    # Total credentials with a captured webhook URL
    try:
        total_captured = await async_execute(
            db.table("discovered_credentials")
            .select("id", count="exact")
            .not_.is_("meta->>webhook_url", "null")
        )
        total_pinned = await async_execute(
            db.table("discovered_credentials")
            .select("id", count="exact")
            .not_.is_("meta->>pinned_webhook_msg_id", "null")
        )
        recent = await async_execute(
            db.table("discovered_credentials")
            .select("bot_username, meta")
            .not_.is_("meta->>webhook_url", "null")
            .order("updated_at", desc=True)
            .limit(400)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    captured_count = total_captured.count or 0
    pinned_count = total_pinned.count or 0
    coverage_pct = (pinned_count / captured_count * 100) if captured_count else 0.0

    # Aggregate top C2 hostnames
    host_counter: Counter = Counter()
    for row in recent.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue
        try:
            host = urlparse(url).hostname
            if host:
                host_counter[host] += 1
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue

    top_hosts = host_counter.most_common(10)

    lines = [
        "📌 **Webhook Takeover Metrics**",
        "",
        f"• Credentials with captured webhook: {captured_count}",
        f"• Pinned in-topic: {pinned_count} ({coverage_pct:.1f}% coverage)",
        "",
    ]
    if top_hosts:
        lines.append("**Top C2 hosts (recent 400):**")
        for host, count in top_hosts:
            lines.append(f"• `{host}` × {count}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "captured": captured_count,
        "pinned": pinned_count,
        "top_hosts": len(top_hosts),
    }

TAKEOVER_SPIKE_THRESHOLD = 20

TAKEOVER_SPIKE_WINDOW_SECONDS = 3600  # 1 hour

EXFIL_LATENCY_SAMPLE_SIZE = 1000

@app.task(name="flow.takeover_spike_check")
def takeover_spike_check():
    """Alert monitor group when webhook takeover rate spikes.

    Counts ``audit_logs`` rows with ``event_type='webhook.takeover'`` in the
    last hour. If the count exceeds ``TAKEOVER_SPIKE_THRESHOLD`` we treat it
    as a likely mass-exposure event and broadcast a warning. Silent when the
    count is at or below threshold to keep the channel quiet.

    Assumes ``webhook.takeover`` audit events are persisted to ``audit_logs``.
    If persistence is not enabled the count will be zero and no alert fires
    — that's still a valid observability signal (either no takeovers or the
    audit persistence gate needs to be widened).
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_takeover_spike_check_logic())

async def _takeover_spike_check_logic() -> dict:
    from datetime import datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=TAKEOVER_SPIKE_WINDOW_SECONDS)
    cutoff_iso = cutoff.isoformat()

    try:
        response = await async_execute(
            db.table("audit_logs")
            .select("id", count="exact")
            .eq("event_type", "webhook.takeover")
            .gte("timestamp", cutoff_iso)
            .limit(1)  # we only need the count, not the rows
        )
    except Exception as e:
        logger.warning(f"[TakeoverSpike] audit_logs query failed: {e}")
        return {"status": "db_query_failed", "error": str(e)[:200]}

    count = int(response.count or 0)
    logger.info(
        f"[TakeoverSpike] {count} webhook.takeover events in last "
        f"{TAKEOVER_SPIKE_WINDOW_SECONDS // 60} min (threshold={TAKEOVER_SPIKE_THRESHOLD})"
    )

    if count <= TAKEOVER_SPIKE_THRESHOLD:
        return {"status": "ok", "count": count, "threshold": TAKEOVER_SPIKE_THRESHOLD}

    alert = (
        "🚨 Webhook Takeover Spike\n"
        f"{count} takeovers in the last hour (threshold: {TAKEOVER_SPIKE_THRESHOLD}).\n"
        "Likely mass exposure event — investigate."
    )
    try:
        await get_broadcaster().send_log(alert)
    except Exception as e:
        logger.warning(f"[TakeoverSpike] broadcast failed: {e}")
        return {"status": "broadcast_failed", "count": count, "error": str(e)[:200]}

    return {"status": "alerted", "count": count, "threshold": TAKEOVER_SPIKE_THRESHOLD}

@app.task(name="flow.exfil_latency_report")
def exfil_latency_report():
    """Broadcast P50/P95/P99 exfiltration→broadcast latency for the last N messages.

    Sample size is capped at ``EXFIL_LATENCY_SAMPLE_SIZE`` (1000). If the
    ``broadcasted_at`` column exists (migration
    ``20260803000001_broadcasted_at.sql`` applied), we compute TRUE latency
    as ``broadcasted_at − created_at``. Otherwise we fall back to the
    upper-bound proxy ``NOW − created_at`` for rows where
    ``is_broadcasted = True``. The report body labels which mode was used.
    """
    from app.workers.celery_app import get_worker_loop
    return get_worker_loop().run_until_complete(_exfil_latency_report_logic())

async def _exfil_latency_report_logic() -> dict:
    import statistics
    from datetime import datetime

    # Attempt the true-latency query first (requires broadcasted_at column).
    true_latency_mode = True
    try:
        response = await async_execute(
            db.table("exfiltrated_messages")
            .select("id, created_at, is_broadcasted, broadcasted_at")
            .order("created_at", desc=True)
            .limit(EXFIL_LATENCY_SAMPLE_SIZE)
        )
    except Exception as e:
        if "broadcasted_at" in str(e):
            true_latency_mode = False
            try:
                response = await async_execute(
                    db.table("exfiltrated_messages")
                    .select("id, created_at, is_broadcasted, broadcast_claimed_at")
                    .order("created_at", desc=True)
                    .limit(EXFIL_LATENCY_SAMPLE_SIZE)
                )
            except Exception as e2:
                logger.warning(f"[ExfilLatency] db query failed: {e2}")
                return {"status": "db_query_failed", "error": str(e2)[:200]}
        else:
            logger.warning(f"[ExfilLatency] db query failed: {e}")
            return {"status": "db_query_failed", "error": str(e)[:200]}

    rows = response.data or []
    if not rows:
        return {"status": "no_data", "sample_size": 0}

    now = datetime.now(UTC)
    latencies: list[float] = []
    broadcasted = 0
    pending = 0

    def _parse_iso(raw) -> datetime | None:
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    for row in rows:
        created_at = _parse_iso(row.get("created_at"))
        if not created_at:
            continue

        if row.get("is_broadcasted"):
            broadcasted += 1
            if true_latency_mode:
                broadcasted_at = _parse_iso(row.get("broadcasted_at"))
                if broadcasted_at:
                    # TRUE latency — end-to-end pipeline time
                    latencies.append((broadcasted_at - created_at).total_seconds())
                else:
                    # Legacy row with no broadcasted_at — skip so mean stays honest
                    continue
            else:
                # Proxy — upper bound
                latencies.append((now - created_at).total_seconds())
        else:
            pending += 1

    if not latencies:
        msg = (
            "📊 **Exfil Latency Report**\n"
            f"Sample: {len(rows)} messages — none broadcasted yet.\n"
            f"Pending: {pending}"
        )
        try:
            await get_broadcaster().send_log(msg)
        except Exception as e:
            logger.warning(f"[ExfilLatency] broadcast failed: {e}")
        return {"status": "no_broadcasted_rows", "sample_size": len(rows), "pending": pending}

    latencies.sort()

    def _pct(sorted_values: list[float], p: float) -> float:
        """Nearest-rank percentile for a sorted list (matches operational reporting)."""
        if not sorted_values:
            return 0.0
        idx = min(len(sorted_values) - 1, max(0, int(round(p / 100.0 * len(sorted_values))) - 1))
        return sorted_values[idx]

    p50 = _pct(latencies, 50)
    p95 = _pct(latencies, 95)
    p99 = _pct(latencies, 99)
    mean = statistics.fmean(latencies)

    mode_label = (
        "TRUE (broadcasted_at − created_at)"
        if true_latency_mode
        else "UPPER-BOUND PROXY (NOW − created_at, schema lacks broadcasted_at)"
    )
    msg = (
        "📊 **Exfil Latency Report**\n"
        f"Sample: last {len(rows)} messages "
        f"(broadcasted={broadcasted}, pending={pending}, latency_sample={len(latencies)})\n"
        f"Mode: {mode_label}\n"
        f"• P50: {p50:,.1f}s\n"
        f"• P95: {p95:,.1f}s\n"
        f"• P99: {p99:,.1f}s\n"
        f"• Mean: {mean:,.1f}s"
    )
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        logger.warning(f"[ExfilLatency] broadcast failed: {e}")
        return {
            "status": "broadcast_failed",
            "sample_size": len(rows),
            "p50": p50, "p95": p95, "p99": p99,
            "error": str(e)[:200],
        }

    return {
        "status": "ok",
        "sample_size": len(rows),
        "broadcasted": broadcasted,
        "pending": pending,
        "p50_seconds": p50,
        "p95_seconds": p95,
        "p99_seconds": p99,
        "mean_seconds": mean,
    }

@app.task(name="flow.source_quality_report")
def source_quality_report():
    """Rank OSINT sources by validated + live + message-rich yield.
    Broadcasts a per-source scorecard to the monitor group.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_source_quality_report_logic())

async def _source_quality_report_logic() -> dict:
    from collections import defaultdict

    try:
        rows = await async_execute(
            db.table("discovered_credentials")
            .select("source, status, meta")
            .limit(5000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    per_source: dict = defaultdict(
        lambda: {
            "total": 0,
            "active": 0,
            "revoked": 0,
            "with_webhook": 0,
            "with_messages": 0,
        }
    )
    for r in rows.data or []:
        src = (r.get("source") or "unknown").split(":", 1)[0]
        meta = r.get("meta") or {}
        status = r.get("status")
        per_source[src]["total"] += 1
        if status == "active":
            per_source[src]["active"] += 1
        elif status == "revoked":
            per_source[src]["revoked"] += 1
        if meta.get("webhook_url"):
            per_source[src]["with_webhook"] += 1
        # message evidence: last_scrape_reason == 'success' OR message_count in meta
        if meta.get("last_scrape_reason") == "success" or meta.get("total_messages_scraped", 0) > 0:
            per_source[src]["with_messages"] += 1

    # Score = active_rate * 0.5 + webhook_rate * 0.2 + message_rate * 0.3
    scored: list[tuple[str, float, dict]] = []
    for src, stats in per_source.items():
        total = stats["total"]
        if total < 3:
            continue
        active_rate = stats["active"] / total
        webhook_rate = stats["with_webhook"] / total
        message_rate = stats["with_messages"] / total
        score = active_rate * 0.5 + webhook_rate * 0.2 + message_rate * 0.3
        scored.append((src, score, stats))
    scored.sort(key=lambda x: x[1], reverse=True)

    lines = ["📊 **Source Quality Scorecard**", ""]
    lines.append("Score: 0.5×active_rate + 0.2×webhook_rate + 0.3×message_rate")
    lines.append("")
    for src, score, stats in scored[:15]:
        lines.append(
            f"• `{src}` score={score:.2f} n={stats['total']} "
            f"active={stats['active']} webhook={stats['with_webhook']} "
            f"msg={stats['with_messages']}"
        )

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {"status": "ok", "sources_ranked": len(scored)}

@app.task(name="flow.media_duplicate_report")
def media_duplicate_report():
    """Broadcast summary of duplicate media SHA-256s across bots — a same
    file being sent by many bots suggests a common operator or automated payload.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_media_duplicate_report_logic())

async def _media_duplicate_report_logic() -> dict:
    try:
        # Group by sha256 with count > 1
        res = await async_execute(
            db.table("media_hashes")
            .select("sha256, credential_id, media_type")
            .not_.like("sha256", "__failed__%")
            .limit(5000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    from collections import defaultdict

    by_hash: dict = defaultdict(set)
    hash_type: dict = {}
    for row in res.data or []:
        sha = row.get("sha256")
        cred = row.get("credential_id")
        if not sha or not cred:
            continue
        by_hash[sha].add(cred)
        hash_type[sha] = row.get("media_type")

    duplicates = [
        (sha, len(creds), hash_type.get(sha))
        for sha, creds in by_hash.items()
        if len(creds) >= 2
    ]
    duplicates.sort(key=lambda x: x[1], reverse=True)

    if not duplicates:
        with contextlib.suppress(Exception):
            await get_broadcaster().send_log(
                "🔎 **Media Duplicate Report** — no shared media across bots yet."
            )
        return {"status": "no_duplicates"}

    lines = [
        "🔎 **Media Duplicate Report**",
        "Same file (SHA-256) sent by multiple compromised bots — likely common operator.",
        "",
    ]
    for sha, count, mtype in duplicates[:15]:
        lines.append(f"• `{sha[:16]}...` ({mtype}) × {count} bots")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {"status": "ok", "duplicate_hashes": len(duplicates)}

@app.task(name="flow.attribution_graph_report")
def attribution_graph_report():
    """Link Telegram user_ids across multiple compromised bots.

    Identifies serial victims (same person interacting with many stolen bots)
    and coordinated operator patterns (single user_id driving traffic into a
    cluster of bots owned by the same C2). Broadcasts top findings.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_attribution_graph_report_logic())

async def _attribution_graph_report_logic() -> dict:
    from collections import defaultdict

    from app.services.findings import (
        cross_bot_pattern_candidates,
        persist_candidates,
        pseudonymize_subject,
    )

    # Pull sender_user_id → credential_id pairs (all-time, bounded to recent 50k rows)
    try:
        res = await async_execute(
            db.table("exfiltrated_messages")
            .select("id,sender_user_id,credential_id,created_at")
            .not_.is_("sender_user_id", "null")
            .order("created_at", desc=True)
            .limit(50000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    rows = res.data or []
    if not rows:
        return {"status": "no_data_with_sender_user_id"}

    finding_count = 0
    try:
        cross_bot_findings = cross_bot_pattern_candidates(rows)
        await persist_candidates(cross_bot_findings)
        finding_count = len(cross_bot_findings)
    except Exception as exc:
        logger.warning("[AttributionGraph] persistent finding upsert failed: %s", exc)

    # Group: user_id → set of credential_ids they've interacted with
    user_bots: dict = defaultdict(set)
    for r in rows:
        uid = r.get("sender_user_id")
        cid = r.get("credential_id")
        if uid and cid:
            user_bots[uid].add(cid)

    # Filter to users who've hit 2+ different bots
    multi_bot_users = {
        uid: creds for uid, creds in user_bots.items() if len(creds) >= 2
    }

    if not multi_bot_users:
        msg = (
            "🕸️ **Attribution Graph Report**\n\n"
            f"Analyzed {len(rows):,} messages from {len(user_bots):,} unique users.\n"
            "No users found interacting with 2+ captured bots yet. "
            "Graph will populate as new sender_user_id data flows in."
        )
        with contextlib.suppress(Exception):
            await get_broadcaster().send_log(msg)
        return {
            "status": "no_multi_bot_users",
            "total_messages_scanned": len(rows),
            "unique_users": len(user_bots),
        }

    # Rank by number of distinct bots
    ranked = sorted(multi_bot_users.items(), key=lambda x: len(x[1]), reverse=True)

    # Also correlate with C2 operator info
    cred_ids_of_interest = set()
    for _uid, creds in ranked[:20]:
        cred_ids_of_interest.update(creds)

    # Fetch webhook_url for those credentials
    cred_c2: dict = {}
    if cred_ids_of_interest:
        try:
            cred_res = await async_execute(
                db.table("discovered_credentials")
                .select("id, bot_username, meta")
                .in_("id", list(cred_ids_of_interest)[:200])
            )
            for c in cred_res.data or []:
                meta = c.get("meta") or {}
                cred_c2[c["id"]] = {
                    "bot": c.get("bot_username") or "?",
                    "c2": meta.get("webhook_url") or "none",
                }
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

    lines = [
        "🕸️ **Attribution Graph Report**",
        "",
        f"Analyzed {len(rows):,} messages from {len(user_bots):,} unique users.",
        f"**{len(multi_bot_users)} users** interact with 2+ captured bots (serial victims or operators).",
        "",
        "**Top cross-bot users:**",
    ]
    for uid, creds in ranked[:10]:
        subject_pseudonym = pseudonymize_subject(uid)
        bot_names = [cred_c2.get(c, {}).get("bot", c[:8]) for c in list(creds)[:5]]
        c2_hosts = set()
        for c in creds:
            url = cred_c2.get(c, {}).get("c2", "")
            if url and url != "none":
                try:
                    from urllib.parse import urlparse
                    c2_hosts.add(urlparse(url).hostname or "?")
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
        c2_str = f" → C2: {', '.join(list(c2_hosts)[:3])}" if c2_hosts else ""
        lines.append(
            f"• `subject:{subject_pseudonym}` × {len(creds)} bots "
            f"({', '.join(bot_names[:3])}{'...' if len(bot_names) > 3 else ''})"
            f"{c2_str}"
        )

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "total_messages_scanned": len(rows),
        "unique_users": len(user_bots),
        "multi_bot_users": len(multi_bot_users),
        "top_user_bots_count": len(ranked[0][1]) if ranked else 0,
        "findings_upserted": finding_count,
    }
