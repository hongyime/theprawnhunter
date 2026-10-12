"""Scanner task implementations for dockerhub."""

from ..common import *

@app.task(name="scanner.scan_dockerhub", autoretry_for=(Exception,), retry_backoff=True, max_retries=2)
def scan_dockerhub():
    """
    Search Docker Hub for 'telegram-bot' images and inspect their manifests/config
    for hardcoded ENV variables containing bot tokens.
    """
    return _run_sync(_scan_dockerhub_async())

async def _scan_dockerhub_async():
    if redis_client.get("system:paused"):
        return "System Paused"

    import httpx

    from app.services.scanners import TOKEN_PATTERN, _is_valid_token

    logger.info("🐳 [DockerHub] starting image manifest scan...")
    await _send_log_async("🐳 [DockerHub] scanning public image manifests for ENV leaks...")

    results = []
    queries = ["telegram bot", "telegram-bot", "tgbot", "tg-bot"]
    seen_images = set()

    # Free, unauthenticated Docker registry API endpoints
    SEARCH_URL = "https://hub.docker.com/v2/search/repositories"  # noqa: N806 — function-scoped constant
    AUTH_URL = "https://auth.docker.io/token"  # noqa: N806 — function-scoped constant
    REGISTRY_URL = "https://registry-1.docker.io/v2"  # noqa: N806 — function-scoped constant

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        # Step 1: Find images via Search API
        for q in queries:
            try:
                res = await client.get(SEARCH_URL, params={"query": q, "page_size": 20})
                if res.status_code != 200:
                    continue
                data = res.json()
                for repo in data.get("results", []):
                    img_name = repo.get("repo_name")
                    if img_name:
                        seen_images.add(img_name)
            except Exception as e:
                logger.warning(f"    [DockerHub] search failed for {q}: {e}")

        logger.info(f"    [DockerHub] Found {len(seen_images)} repositories to inspect.")

        # Step 2: Fetch manifest config for each image
        for img_name in list(seen_images)[:50]:  # Cap at 50 per run to respect pull rate limits
            if "/" not in img_name:
                img_name = f"library/{img_name}" # Official images

            # Dedup check
            seen_key = f"dockerhub:seen:{img_name}"
            try:
                if redis_client.exists(seen_key):
                    continue
            except Exception as _swallowed:
                logger.debug(f"[suppressed] {_swallowed}")

            try:
                # 2a. Get anonymous bearer token for the specific repo
                auth_res = await client.get(AUTH_URL, params={"service": "registry.docker.io", "scope": f"repository:{img_name}:pull"})
                if auth_res.status_code != 200:
                    continue
                token = auth_res.json().get("token")
                if not token:
                    continue

                headers = {
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json"
                }

                # 2b. Get manifest for the 'latest' tag
                manifest_res = await client.get(f"{REGISTRY_URL}/{img_name}/manifests/latest", headers=headers)
                if manifest_res.status_code != 200:
                    continue
                manifest_data = manifest_res.json()

                # 2c. Extract config digest
                config_digest = manifest_data.get("config", {}).get("digest")
                if not config_digest:
                    continue

                # 2d. Fetch the config blob
                blob_res = await client.get(f"{REGISTRY_URL}/{img_name}/blobs/{config_digest}", headers=headers)
                if blob_res.status_code != 200:
                    continue
                config_data = blob_res.json()

                # 2e. Search the 'Env' and 'Cmd' arrays in the container config
                container_config = config_data.get("config", {})
                env_vars = container_config.get("Env", [])
                cmds = container_config.get("Cmd", [])

                text_to_scan = "\n".join(env_vars + cmds)
                found = TOKEN_PATTERN.findall(text_to_scan)

                for t in found:
                    if _is_valid_token(t):
                        results.append({
                            "token": t,
                            "meta": {"source": "dockerhub", "image": img_name}
                        })

                with contextlib.suppress(Exception):
                    redis_client.setex(seen_key, 7 * 86400, "1")

            except Exception as e:
                logger.debug(f"    [DockerHub] manifest pull failed for {img_name}: {e}")

            await asyncio.sleep(1) # Courtesy delay

    if results:
        saved = await _save_credentials_async(results, "dockerhub")
        msg = f"DockerHub: enqueued {saved} tokens"
    else:
        msg = "DockerHub: 0 matches"

    await _send_log_async(f"🏁 [DockerHub] {msg}")
    return msg
