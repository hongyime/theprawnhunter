"""VirusTotal host-reputation enrichment for captured credentials."""

from .common import *

from app.infrastructure.database import execute_async

VIRUSTOTAL_RECHECK_DAYS = 7
VIRUSTOTAL_MAX_PER_RUN = 50


@app.task(name="flow.virustotal_enrich")
def virustotal_enrich(limit: int = VIRUSTOTAL_MAX_PER_RUN):
    return _run_sync(_virustotal_enrich_async(limit))


def _host_from_url(url: str) -> str | None:
    try:
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname
        return host.lower() if host else None
    except Exception:  # noqa: BLE001
        return None


def _is_stale(meta: dict) -> bool:
    checked = meta.get("vt_checked_at")
    if not checked:
        return True
    try:
        from datetime import UTC, datetime, timedelta

        when = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
        return when < datetime.now(UTC) - timedelta(days=VIRUSTOTAL_RECHECK_DAYS)
    except Exception:  # noqa: BLE001
        return True


async def _virustotal_enrich_async(limit: int) -> str:
    from app.services.virustotal_srv import virustotal

    if not virustotal.enabled:
        return "virustotal:disabled"

    response = await execute_async(
        db.table("discovered_credentials").select("id,meta").limit(500)
    )
    checked = malicious = 0
    for row in response.data or []:
        if checked >= limit or virustotal.budget_exhausted():
            break
        meta = row.get("meta") or {}
        if not isinstance(meta, dict):
            continue
        webhook = meta.get("webhook_url")
        if not webhook or not _is_stale(meta):
            continue
        host = _host_from_url(str(webhook))
        if not host:
            continue

        is_ip = all(part.isdigit() for part in host.split(".")) and host.count(".") == 3
        verdict = await virustotal.ip_report(host) if is_ip else await virustotal.domain_report(host)
        if verdict is None:
            continue

        merged = dict(meta)
        merged["vt_verdict"] = {
            "malicious": verdict["malicious"],
            "suspicious": verdict["suspicious"],
            "reputation": verdict.get("reputation"),
        }
        merged["vt_checked_at"] = verdict["checked_at"]
        await execute_async(
            db.table("discovered_credentials").update({"meta": merged}).eq("id", row["id"])
        )
        checked += 1
        if verdict["malicious"] >= 2:
            malicious += 1

    logger.info(
        f"[VirusTotal] enriched {checked} host(s); {malicious} flagged malicious"
    )
    return f"virustotal:checked={checked},malicious={malicious}"
