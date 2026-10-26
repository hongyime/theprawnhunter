"""Daily third-party credential health check with transition-only alerts."""

from .common import *

from app.workers.celery_app import _run_sync  # noqa: E402

API_KEY_STATE_PREFIX = "api_key_health:state:"
API_KEY_STATE_TTL_SECONDS = 30 * 24 * 3600


@app.task(name="flow.api_key_health")
def api_key_health():
    return _run_sync(_api_key_health_async())


async def _api_key_health_async() -> str:
    from app.services.api_key_health import check_all
    from app.workers.tasks.flow_tasks import get_broadcaster

    try:
        results = await check_all()
    except Exception as error:  # noqa: BLE001 - health checks must never crash the worker
        logger.warning(f"[ApiKeyHealth] check run failed: {error}")
        return "api_key_health:error"

    checked = failing = 0
    transitions: list[tuple[dict, str]] = []
    for row in results:
        if row["detail"] == "not configured":
            continue
        checked += 1
        if not row["ok"]:
            failing += 1
        key = f"{API_KEY_STATE_PREFIX}{row['provider']}"
        try:
            previous = redis_client.get(key)
        except Exception:  # noqa: BLE001
            previous = None
        current = "ok" if row["ok"] else "fail"
        if previous != current:
            try:
                redis_client.setex(key, API_KEY_STATE_TTL_SECONDS, current)
            except Exception:  # noqa: BLE001
                pass
            if current == "fail" or previous == "fail":
                transitions.append((row, current))

    for row, state in transitions:
        try:
            if state == "fail":
                message = (
                    f"API key health: {row['provider']} FAILED — {row['detail']}. "
                    "Ingestion from that source is paused; everything else keeps running."
                )
            else:
                message = f"API key health: {row['provider']} recovered"
            await get_broadcaster().send_log(message)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"[ApiKeyHealth] alert failed for {row['provider']}: {error}")

    logger.info(f"[ApiKeyHealth] checked={checked} failing={failing} transitions={len(transitions)}")
    return f"api_key_health:checked={checked},failing={failing}"
