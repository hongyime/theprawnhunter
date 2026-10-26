"""Live checks for every configured third-party credential.

Each check is independent and never raises: a dead key yields ``ok=False`` with a
short reason so callers can flag it without affecting ingestion.
"""

from __future__ import annotations

import base64
import hashlib

import httpx

from app.core.config import settings

TIMEOUT = 15.0


def _mask(value: str | None) -> str:
    if not value:
        return ""
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "…"


async def _get(client: httpx.AsyncClient, url: str, headers: dict | None = None):
    return await client.get(url, headers=headers or {})


async def check_github(client: httpx.AsyncClient) -> list[dict]:
    raw = getattr(settings, "GITHUB_TOKENS", None) or getattr(settings, "GITHUB_TOKEN", None) or ""
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return [{"provider": "github", "ok": False, "detail": "no token configured"}]
    results = []
    for index, token in enumerate(tokens, 1):
        response = await _get(
            client,
            "https://api.github.com/rate_limit",
            {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        ok = response.status_code == 200
        detail = "ok"
        if ok:
            data = response.json().get("resources", {}).get("search", {})
            detail = f"search {data.get('remaining')}/{data.get('limit')}"
        else:
            detail = f"HTTP {response.status_code}"
        results.append(
            {"provider": f"github#{index}", "ok": ok, "detail": detail, "fingerprint": hashlib.sha256(token.encode()).hexdigest()[:8]}
        )
    return results


async def check_gitlab(client: httpx.AsyncClient) -> list[dict]:
    token = getattr(settings, "GITLAB_TOKEN", None)
    if not token:
        return [{"provider": "gitlab", "ok": False, "detail": "not configured"}]
    response = await _get(client, "https://gitlab.com/api/v4/user", {"PRIVATE-TOKEN": token})
    return [{"provider": "gitlab", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


async def check_exa(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "EXA_API_KEY", None)
    if not key:
        return [{"provider": "exa", "ok": False, "detail": "not configured"}]
    response = await client.post(
        "https://api.exa.ai/search",
        headers={"x-api-key": key, "Content-Type": "application/json"},
        json={"query": "test", "numResults": 1},
    )
    return [{"provider": "exa", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


async def check_shodan(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "SHODAN_KEY", None)
    if not key:
        return [{"provider": "shodan", "ok": False, "detail": "not configured"}]
    response = await _get(client, f"https://api.shodan.io/api-info?key={key}")
    detail = "ok"
    if response.status_code == 200:
        data = response.json()
        detail = f"query credits {data.get('query_credits')}"
    return [{"provider": "shodan", "ok": response.status_code == 200, "detail": detail}]


async def check_netlas(client: httpx.AsyncClient) -> list[dict]:
    results = []
    for name in ("NETLAS_API_KEY_1", "NETLAS_API_KEY_2"):
        key = getattr(settings, name, None)
        if not key:
            continue
        response = await _get(client, f"https://app.netlas.io/api/domains/?q=telegram&apikey={key}")
        results.append(
            {"provider": name.lower(), "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}
        )
    return results or [{"provider": "netlas", "ok": False, "detail": "not configured"}]


async def check_urlscan(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "URLSCAN_KEY", None)
    if not key:
        return [{"provider": "urlscan", "ok": False, "detail": "not configured"}]
    response = await _get(client, "https://urlscan.io/user/quotas/", {"API-Key": key})
    return [{"provider": "urlscan", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


async def check_postman(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "POSTMAN_API_KEY", None)
    if not key:
        return [{"provider": "postman", "ok": False, "detail": "not configured"}]
    response = await _get(client, "https://api.getpostman.com/me", {"X-Api-Key": key})
    return [{"provider": "postman", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


async def check_publicwww(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "PUBLICWWW_KEY", None)
    if not key:
        return [{"provider": "publicwww", "ok": False, "detail": "not configured"}]
    response = await _get(client, f"https://publicwww.com/profile/?key={key}")
    return [{"provider": "publicwww", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


async def check_virustotal(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "VIRUSTOTAL_API_KEY", None)
    if not key:
        return [{"provider": "virustotal", "ok": False, "detail": "not configured"}]
    response = await _get(client, "https://www.virustotal.com/api/v3/users/me", {"x-apikey": key})
    return [{"provider": "virustotal", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


async def check_google(client: httpx.AsyncClient) -> list[dict]:
    key = getattr(settings, "GOOGLE_SEARCH_KEY", None)
    cx = getattr(settings, "GOOGLE_CSE_ID", None)
    if not key or not cx:
        return [{"provider": "google_cse", "ok": False, "detail": "not configured"}]
    response = await _get(
        client,
        f"https://www.googleapis.com/customsearch/v1?key={key}&cx={cx}&q=test&num=1",
    )
    detail = "ok" if response.status_code == 200 else response.text[:80]
    return [{"provider": "google_cse", "ok": response.status_code == 200, "detail": detail}]


async def check_fofa(client: httpx.AsyncClient) -> list[dict]:
    email = getattr(settings, "FOFA_EMAIL", None)
    key = getattr(settings, "FOFA_KEY", None)
    if not email or not key:
        return [{"provider": "fofa", "ok": False, "detail": "not configured"}]
    response = await _get(client, f"https://fofa.info/api/v1/info/my?email={email}&key={key}")
    ok = response.status_code == 200 and "error" not in response.text.lower()
    return [{"provider": "fofa", "ok": ok, "detail": response.text[:80]}]


async def check_bitbucket(client: httpx.AsyncClient) -> list[dict]:
    user = getattr(settings, "BITBUCKET_USER", None)
    token = getattr(settings, "BITBUCKET_API_TOKEN", None)
    if not (user and token):
        return [{"provider": "bitbucket", "ok": False, "detail": "not configured"}]
    auth = base64.b64encode(f"{user}:{token}".encode()).decode()
    response = await _get(client, "https://api.bitbucket.org/2.0/workspaces", {"Authorization": f"Basic {auth}"})
    return [{"provider": "bitbucket", "ok": response.status_code == 200, "detail": f"HTTP {response.status_code}"}]


CHECKS = (
    check_github,
    check_gitlab,
    check_exa,
    check_shodan,
    check_netlas,
    check_urlscan,
    check_postman,
    check_publicwww,
    check_virustotal,
    check_google,
    check_fofa,
    check_bitbucket,
)


async def check_all() -> list[dict]:
    results: list[dict] = []
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        for check in CHECKS:
            try:
                results.extend(await check(client))
            except Exception as error:  # noqa: BLE001 - one bad check must not hide the rest
                results.append({"provider": check.__name__.removeprefix("check_"), "ok": False, "detail": f"{type(error).__name__}: {error}"})
    return results
