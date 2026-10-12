"""Findings flow task definitions."""

from .common import *

@app.task(name="flow.reclassify_dark_matter")
def reclassify_dark_matter(max_credentials: int = 500):
    """Sweep credentials that are neither 'active' nor 'revoked' — call getMe
    for each and reclassify. Live bots → active; 401/404 tokens → revoked.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _reclassify_dark_matter_logic(max_credentials)
    )

async def _reclassify_dark_matter_logic(max_credentials: int) -> dict:
    import httpx

    from app.core.security import security

    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_token, status")
            .not_.in_("status", ["active", "revoked"])
            .limit(max_credentials)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    to_active: list[str] = []
    to_revoke: list[str] = []
    inspected = 0

    async with httpx.AsyncClient(timeout=8.0) as client:
        for row in res.data or []:
            enc = row.get("bot_token")
            if not enc:
                continue
            try:
                token = security.decrypt(enc).strip()
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")
                continue
            inspected += 1
            try:
                r = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                if r.status_code == 200 and (r.json() or {}).get("ok"):
                    to_active.append(row["id"])
                elif r.status_code in (401, 404):
                    to_revoke.append(row["id"])
            except Exception as _swallowed:
                # Ambiguous — leave alone this pass
                logger.debug(f"[suppressed] {_swallowed}")
                continue

    # Apply updates in batches
    for cred_id in to_active:
        with contextlib.suppress(Exception):
            await async_execute(
                db.table("discovered_credentials")
                .update({"status": "active"})
                .eq("id", cred_id)
            )
    for cred_id in to_revoke:
        with contextlib.suppress(Exception):
            await _mark_credential_revoked(cred_id, "dark_matter_reclassify")

    msg = (
        f"🔄 Dark-matter reclassify — inspected {inspected}, "
        f"reactivated {len(to_active)}, revoked {len(to_revoke)}"
    )
    logger.info(msg)
    with contextlib.suppress(Exception):
        await get_broadcaster().send_log(msg)

    return {
        "status": "ok",
        "inspected": inspected,
        "reactivated": len(to_active),
        "revoked": len(to_revoke),
    }

@app.task(name="flow.produce_findings")
def produce_findings(credential_limit: int = 2000, message_limit: int = 50000):
    """Idempotently compress a bounded recent window into persistent findings."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _produce_findings_logic(credential_limit, message_limit)
    )

async def _produce_findings_logic(
    credential_limit: int = 2000, message_limit: int = 50000
) -> dict:
    from app.services.findings import produce_recent_findings

    try:
        return await produce_recent_findings(
            credential_limit=credential_limit,
            message_limit=message_limit,
        )
    except Exception as exc:
        logger.exception("[Findings] producer run failed")
        return {"status": "failed", "error": str(exc)[:300]}

@app.task(name="flow.route_finding_deltas")
def route_finding_deltas():
    """Route immediate, policy-matching material finding changes."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_route_finding_alerts_logic("immediate"))

@app.task(name="flow.daily_findings_digest")
def daily_findings_digest():
    """Send the policy-filtered, grouped daily Top Findings digest."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_route_finding_alerts_logic("daily"))

@app.task(name="flow.weekly_finding_alerts")
def weekly_finding_alerts():
    """Send optional weekly finding routes followed by delivery coverage."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_weekly_finding_alerts_logic())

async def _route_finding_alerts_logic(cadence: str) -> dict:
    from app.core.config import settings
    if not settings.FINDING_ALERTS_ENABLED:
        logger.info("[FindingAlerts] skipping %s routing — FINDING_ALERTS_ENABLED=False", cadence)
        return {"status": "skipped", "cadence": cadence, "reason": "FINDING_ALERTS_ENABLED=False"}

    from app.services.finding_alerts import route_finding_alerts

    try:
        return await route_finding_alerts(cadence)
    except Exception as exc:
        logger.exception("[FindingAlerts] %s routing failed", cadence)
        return {"status": "failed", "cadence": cadence, "error": str(exc)[:300]}

async def _weekly_finding_alerts_logic() -> dict:
    from app.core.config import settings
    if not settings.FINDING_ALERTS_ENABLED:
        logger.info("[FindingAlerts] skipping weekly routing — FINDING_ALERTS_ENABLED=False")
        return {"status": "skipped", "reason": "FINDING_ALERTS_ENABLED=False"}

    from app.services.finding_alerts import route_finding_alerts, weekly_alert_coverage

    try:
        routed = await route_finding_alerts("weekly")
        coverage = await weekly_alert_coverage()
        return {"status": "ok", "routed": routed, "coverage": coverage}
    except Exception as exc:
        logger.exception("[FindingAlerts] weekly routing or coverage failed")
        return {"status": "failed", "error": str(exc)[:300]}
