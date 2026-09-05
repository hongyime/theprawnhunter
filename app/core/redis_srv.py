import redis

from app.core.config import settings


class RedisService:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True # Ensure we get strings back
            )
        return self._client

    def set_cooldown(self, key: str, seconds: int):
        """Sets a cooldown in Redis that expires automatically."""
        if seconds <= 0:
            return
        self.client.set(f"cooldown:{key}", "active", ex=seconds)

    def is_on_cooldown(self, key: str) -> bool:
        """Checks if a key is currently on cooldown."""
        return self.client.exists(f"cooldown:{key}") > 0

    def get_cooldown_remaining(self, key: str) -> int:
        """Returns remaining seconds for a cooldown, or 0."""
        ttl = self.client.ttl(f"cooldown:{key}")
        return max(0, ttl)

    def get_next_rotation_index(self, key: str, max_val: int) -> int:
        """Atomically increments and returns the next index modulo max_val."""
        if max_val <= 0: return 0
        idx = self.client.incr(f"rotation_index:{key}")
        return idx % max_val

    def acquire_lock(self, key: str, ttl_seconds: int, owner: str = "1") -> bool:
        if ttl_seconds <= 0:
            ttl_seconds = 60
        return bool(self.client.set(f"lock:{key}", owner, nx=True, ex=ttl_seconds))

    def release_lock(self, key: str, owner: str = "1"):
        """Release lock only if we still own it (fencing via Lua CAS)."""
        lua = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""
        self.client.eval(lua, 1, f"lock:{key}", owner)

    def incr_key(self, key: str, ttl_seconds: int | None = None) -> int:
        pipe = self.client.pipeline()
        pipe.incr(f"counter:{key}")
        if ttl_seconds:
            pipe.expire(f"counter:{key}", ttl_seconds)
        results = pipe.execute()
        return int(results[0])

    def reset_key(self, key: str):
        self.client.delete(f"counter:{key}")

redis_srv = RedisService()


# ---------------------------------------------------------------------------
# Async helpers — Bot API response cache + probe host cooldown
# ---------------------------------------------------------------------------
# These wrap the sync `redis_srv.client` in `async def` so callers in async
# code (scraper strategies, webhook probes) can `await` them naturally.
# The underlying redis client is sync but calls are local + fast; the async
# signature keeps the call sites clean and lets us swap to `redis.asyncio`
# later without touching consumers.
import contextlib
import json as _json


async def get_cached_getme(bot_id: str) -> dict | None:
    """Return cached Bot API getMe response for `bot_id`, or None on miss."""
    try:
        raw = redis_srv.client.get(f"cache:getme:{bot_id}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        return None


async def set_cached_getme(bot_id: str, data: dict, ttl: int = 3600) -> None:
    """Cache Bot API getMe response for `bot_id` with TTL (default 1h)."""
    if not isinstance(data, dict):
        return
    with contextlib.suppress(Exception):
        redis_srv.client.set(
            f"cache:getme:{bot_id}",
            _json.dumps(data, default=str),
            ex=max(1, int(ttl)),
        )


async def get_cached_getchat(bot_id: str, chat_id: int | str) -> dict | None:
    """Return cached Bot API getChat response for (bot_id, chat_id), or None."""
    try:
        raw = redis_srv.client.get(f"cache:getchat:{bot_id}:{chat_id}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        return None


async def set_cached_getchat(
    bot_id: str, chat_id: int | str, data: dict, ttl: int = 3600
) -> None:
    """Cache Bot API getChat response for (bot_id, chat_id) with TTL (default 1h)."""
    if not isinstance(data, dict):
        return
    with contextlib.suppress(Exception):
        redis_srv.client.set(
            f"cache:getchat:{bot_id}:{chat_id}",
            _json.dumps(data, default=str),
            ex=max(1, int(ttl)),
        )


# Probe cooldown: after 3 failures in 1h we back off from `hostname` for 24h.
_PROBE_FAIL_WINDOW_SECONDS = 3600
_PROBE_FAIL_THRESHOLD = 3
_PROBE_COOLDOWN_SECONDS = 86400


async def probe_host_is_cooling(hostname: str) -> bool:
    """Return True if `hostname` is currently on probe cooldown."""
    if not hostname:
        return False
    try:
        return redis_srv.client.exists(f"probe:cooldown:{hostname}") > 0
    except Exception:
        return False


async def probe_host_mark_failure(hostname: str) -> None:
    """Record a probe failure. After 3 fails in 1h, cool `hostname` for 24h."""
    if not hostname:
        return
    try:
        pipe = redis_srv.client.pipeline()
        pipe.incr(f"probe:fail:{hostname}")
        pipe.expire(f"probe:fail:{hostname}", _PROBE_FAIL_WINDOW_SECONDS)
        count, _ = pipe.execute()
        if int(count) >= _PROBE_FAIL_THRESHOLD:
            redis_srv.client.set(
                f"probe:cooldown:{hostname}",
                "active",
                ex=_PROBE_COOLDOWN_SECONDS,
            )
    except Exception:
        pass
