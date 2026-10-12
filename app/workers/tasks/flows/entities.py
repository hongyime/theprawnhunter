"""Entities flow task definitions."""

from .common import *

@app.task(name="flow.build_entity_graph")
def build_entity_graph(credential_limit: int = 2000, evidence_limit: int = 50000):
    """Idempotently materialize the bounded typed evidence graph."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _build_entity_graph_logic(credential_limit, evidence_limit)
    )

async def _build_entity_graph_logic(
    credential_limit: int = 2000, evidence_limit: int = 50000
) -> dict:
    from app.services.entities import produce_entity_graph

    try:
        return await produce_entity_graph(
            credential_limit=credential_limit,
            evidence_limit=evidence_limit,
        )
    except Exception as exc:
        logger.exception("[EntityGraph] producer run failed")
        return {"status": "failed", "error": str(exc)[:300]}

@app.task(name="flow.cluster_c2_operators")
def cluster_c2_operators():
    """Analyze captured webhook URLs and broadcast the top C2 operator clusters
    to the monitor group. Uses same clustering logic as /monitor/operators."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_cluster_c2_operators_logic())

async def _cluster_c2_operators_logic() -> dict:
    from collections import defaultdict
    from urllib.parse import urlparse

    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select(
                "id,bot_username,status,source,meta,created_at,updated_at,"
                "collection_yield_score,chat_member_count"
            )
            .not_.is_("meta->>webhook_url", "null")
            .limit(2000)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    finding_count = 0
    try:
        from app.services.findings import (
            infrastructure_cluster_candidates,
            persist_candidates,
        )

        cluster_findings = infrastructure_cluster_candidates(res.data or [])
        await persist_candidates(cluster_findings)
        finding_count = len(cluster_findings)
    except Exception as exc:
        logger.warning("[C2Clusters] persistent finding upsert failed: %s", exc)

    by_san: dict = defaultdict(list)
    by_org: dict = defaultdict(list)
    by_hostname: dict = defaultdict(list)

    for row in res.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue
        probe = meta.get("webhook_probe") or {}
        try:
            hostname = urlparse(url).hostname or ""
        except Exception:
            hostname = ""

        for san in probe.get("tls_san", []) or []:
            if san.startswith("*."):
                by_san[san].append(row.get("bot_username") or "?")
                break

        for _ip, info in (probe.get("shodan") or {}).items():
            if isinstance(info, dict) and info.get("org"):
                by_org[info["org"]].append(row.get("bot_username") or "?")

        if hostname:
            by_hostname[hostname].append(row.get("bot_username") or "?")

    def _top(bucket: dict, n: int = 8) -> list[tuple[str, int]]:
        items = [(k, len(v)) for k, v in bucket.items() if len(v) >= 2]
        items.sort(key=lambda x: x[1], reverse=True)
        return items[:n]

    lines = ["🎯 **C2 Operator Clusters**", ""]
    lines.append("**Top TLS SAN wildcards (hosted-service tenants):**")
    for san, count in _top(by_san):
        lines.append(f"• `{san}` × {count}")
    lines.append("")
    lines.append("**Top Shodan orgs (network providers):**")
    for org, count in _top(by_org):
        lines.append(f"• {org} × {count}")
    lines.append("")
    lines.append("**Top hostnames (same C2 operator, many bots):**")
    for host, count in _top(by_hostname):
        lines.append(f"• `{host}` × {count}")

    msg = "\n".join(lines)[:3900]
    try:
        await get_broadcaster().send_log(msg)
    except Exception as e:
        return {"status": "broadcast_failed", "error": str(e)[:200]}

    return {
        "status": "ok",
        "san_clusters": len([k for k, v in by_san.items() if len(v) >= 2]),
        "org_clusters": len([k for k, v in by_org.items() if len(v) >= 2]),
        "hostname_clusters": len([k for k, v in by_hostname.items() if len(v) >= 2]),
        "findings_upserted": finding_count,
    }
