"""VirusTotal host-reputation enrichment for captured credentials and URLs."""

from .common import *

from app.infrastructure.database import execute_async

VIRUSTOTAL_RECHECK_DAYS = 7
VIRUSTOTAL_MAX_PER_RUN = 50
VIRUSTOTAL_MAX_URLS_PER_RUN = 100
VIRUSTOTAL_ALERT_TTL_SECONDS = 7 * 24 * 3600


@app.task(name="flow.virustotal_enrich")
def virustotal_enrich(
    limit: int = VIRUSTOTAL_MAX_PER_RUN,
    url_limit: int = VIRUSTOTAL_MAX_URLS_PER_RUN,
):
    return _run_sync(_virustotal_enrich_async(limit, url_limit))


def _host_from_url(url: str) -> str | None:
    try:
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname
        return host.lower() if host else None
    except Exception:  # noqa: BLE001
        return None


def _lookup_kind(host: str) -> str:
    is_ip = host.count(".") == 3 and all(part.isdigit() for part in host.split("."))
    return "ip" if is_ip else "domain"


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


async def _lookup(service, host: str):
    if _lookup_kind(host) == "ip":
        return await service.ip_report(host)
    return await service.domain_report(host)


def _alert_key(host: str) -> str:
    return f"vt:alerted:{host}"


async def _alert_once(host: str, verdict: dict, context: str) -> None:
    try:
        if redis_client.get(_alert_key(host)):
            return
        from app.workers.tasks.flow_tasks import get_broadcaster

        kind = "ip-address" if _lookup_kind(host) == "ip" else "domain"
        await get_broadcaster().send_alert(
            "VirusTotal: malicious infrastructure\n"
            f"Host: {host}\n"
            f"Engines: {verdict.get('malicious', 0)} malicious, "
            f"{verdict.get('suspicious', 0)} suspicious\n"
            f"Seen in: {context}\n"
            f"Details: https://www.virustotal.com/gui/{kind}/{host}"
        )
        redis_client.setex(_alert_key(host), VIRUSTOTAL_ALERT_TTL_SECONDS, "1")
    except Exception as error:  # noqa: BLE001 - alerting is best-effort
        logger.warning(f"[VirusTotal] alert failed for {host}: {error}")


async def _enrich_credentials(service, limit: int) -> tuple[int, int]:
    response = await execute_async(
        db.table("discovered_credentials").select("id,meta").limit(500)
    )
    checked = malicious = 0
    for row in response.data or []:
        if checked >= limit or service.budget_exhausted():
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

        verdict = await _lookup(service, host)
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
        if verdict["malicious"] >= service.alert_threshold:
            malicious += 1
            await _alert_once(host, verdict, f"credential {row['id']}")
    return checked, malicious


async def _enrich_message_urls(service, url_limit: int) -> tuple[int, int]:
    response = await execute_async(
        db.table("exfiltrated_messages")
        .select("id,credential_id,file_meta")
        .order("created_at", desc=True)
        .limit(url_limit)
    )
    checked = malicious = 0
    for row in response.data or []:
        if service.budget_exhausted():
            break
        file_meta = row.get("file_meta") or {}
        urls = file_meta.get("urls") if isinstance(file_meta, dict) else None
        if not isinstance(urls, list) or not urls or file_meta.get("vt_checked_at"):
            continue
        for url in urls[:5]:
            host = _host_from_url(str(url))
            if not host:
                continue
            verdict = await _lookup(service, host)
            if verdict is None:
                continue
            checked += 1
            if verdict["malicious"] >= service.alert_threshold:
                malicious += 1
                await _alert_once(host, verdict, f"message {row['id']}")
            if service.budget_exhausted():
                break
    return checked, malicious


async def _virustotal_enrich_async(limit: int, url_limit: int) -> str:
    try:
        from app.services.virustotal_srv import virustotal

        if not virustotal.enabled:
            return "virustotal:disabled"
        cred_checked, cred_malicious = await _enrich_credentials(virustotal, limit)
        url_checked, url_malicious = await _enrich_message_urls(virustotal, url_limit)
        checked = cred_checked + url_checked
        malicious = cred_malicious + url_malicious
        logger.info(
            f"[VirusTotal] checked {checked} host reference(s); {malicious} malicious"
        )
        return f"virustotal:checked={checked},malicious={malicious}"
    except Exception as error:  # noqa: BLE001 - enrichment must never break the worker
        logger.warning(f"[VirusTotal] enrichment aborted: {error}")
        return "virustotal:error"
