"""Monitor operators resource routes."""

from .common import *

router = APIRouter()

@router.get("/operators")
def get_c2_operators(limit: int = 20):
    """Third-party operator identification via webhook fingerprints.

    Clusters captured webhook URLs by:
    - Root TLS SAN pattern (e.g. *.up.railway.app, *.onrender.com)
    - Shodan organization (from IP resolution)
    - URL path pattern (e.g. /hook/{id}/*, /webhook/bot/*)
    - Hostname base (e.g. ssh.inkognit.org for :port variants)

    Returns clusters ranked by member bot count — largest cluster = most
    prolific third-party operator.
    """
    from collections import defaultdict
    from urllib.parse import urlparse

    try:
        res = (
            db.table("discovered_credentials")
            .select("id, bot_username, meta")
            .not_.is_("meta->>webhook_url", "null")
            .limit(2000)
            .execute()
        )
    except Exception as exc:
        logger.exception("monitor/operators query failed")
        raise HTTPException(status_code=500, detail="Internal error") from exc

    # Multi-dimensional clustering
    by_san: dict = defaultdict(list)
    by_org: dict = defaultdict(list)
    by_hostname: dict = defaultdict(list)
    by_path_pattern: dict = defaultdict(list)

    import re

    for row in res.data or []:
        meta = row.get("meta") or {}
        url = meta.get("webhook_url")
        if not url:
            continue
        bot_username = row.get("bot_username") or "?"
        probe = meta.get("webhook_probe") or {}

        # Parse URL
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname or ""
            path = parsed.path or ""
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")
            continue

        # By TLS SAN root pattern (wildcard cluster)
        for san in probe.get("tls_san", []) or []:
            if san.startswith("*."):
                by_san[san].append({"bot": bot_username, "url": url})
                break  # first wildcard is enough

        # By Shodan org
        shodan = probe.get("shodan") or {}
        orgs = set()
        for _ip, info in shodan.items():
            if isinstance(info, dict) and info.get("org"):
                orgs.add(str(info["org"]))
        for org in orgs:
            by_org[org].append({"bot": bot_username, "url": url})

        # By hostname (strip port)
        if hostname:
            by_hostname[hostname].append({"bot": bot_username, "url": url})

        # By URL path pattern — normalize IDs to {id} placeholder
        pattern = re.sub(r"/\d{6,}", "/{id}", path)
        pattern = re.sub(r"/[a-f0-9]{32,}", "/{hash}", pattern)
        pattern = re.sub(r"/\d+:[A-Za-z0-9_-]{30,}", "/{token}", pattern)
        if pattern and pattern != "/":
            by_path_pattern[pattern].append({"bot": bot_username, "url": url})

    def _rank(bucket: dict, top: int) -> list:
        ranked = [
            {
                "key": k,
                "count": len(v),
                "sample_bots": [x["bot"] for x in v[:5]],
                "sample_url": v[0]["url"] if v else None,
            }
            for k, v in bucket.items()
            if len(v) >= 2
        ]
        ranked.sort(key=lambda x: x["count"], reverse=True)
        return ranked[:top]

    return {
        "total_webhook_rows": len(res.data or []),
        "clusters_by_tls_san": _rank(by_san, limit),
        "clusters_by_shodan_org": _rank(by_org, limit),
        "clusters_by_hostname": _rank(by_hostname, limit),
        "clusters_by_url_path_pattern": _rank(by_path_pattern, limit),
    }
