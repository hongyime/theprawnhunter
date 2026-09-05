from typing import Any

from app.core.config import settings


# supabase-py 2.x (this version) does not expose http_client via ClientOptions.
# The postgrest client's underlying httpx.Client is accessible post-init as:
#   db.postgrest.session
# We patch it after creation to install a transport with retries=1 and a
# keepalive_expiry=30s, so idle connections are dropped before Supabase's
# server-side timeout (~60s) that causes:
#   httpx.RemoteProtocolError: Server disconnected
def _patch_postgrest_session(client: Any) -> None:
    """
    Patch the postgrest httpx.Client transport to add retries=1 on connection reset.
    Also sets keepalive_expiry=30s at the pool level so idle sockets are dropped
    before Supabase's server-side timeout (~60s) that causes:
        httpx.RemoteProtocolError: Server disconnected

    The httpx.Limits API is version-dependent; we patch the connection pool directly
    which is stable across httpx 0.23+.
    """
    try:
        import httpx

        session: httpx.Client = client.postgrest.session
        # Swap transport to add retries=1 (automatic retry on connection reset)
        session._transport = httpx.HTTPTransport(retries=1)
        # Patch the pool inside the new transport for keepalive tuning
        pool = session._transport._pool
        pool._keepalive_expiry = 30.0       # drop idle conn at 30s (server drops at ~60s)
        pool._max_keepalive_connections = 5  # don't hold too many idle sockets
    except Exception:
        # Defensive: if httpx internal structure changes, log and continue.
        import logging
        logging.getLogger("app.core.database").warning(
            "Could not patch postgrest httpx session — RemoteProtocolError mitigation inactive. "
            "Check httpx version compatibility."
        )


class Database:
    _client: Any = None

    @classmethod
    def get_client(cls) -> Any:
        """
        Returns a Supabase client using the SERVICE ROLE KEY.
        This bypasses Row Level Security (RLS) for backend operations.

        IMPORTANT: Never expose this client or key to the frontend.
        Frontend should use SUPABASE_KEY (anon) which respects RLS.

        The postgrest session is patched post-init to prevent
        RemoteProtocolError: Server disconnected on idle connections.
        """
        if cls._client is None:
            from supabase import create_client

            cls._client = create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_ROLE_KEY,
            )
            _patch_postgrest_session(cls._client)
        return cls._client


class LazyDatabaseClient:
    """Defers Supabase client creation until the first DB operation."""

    def _client(self) -> Any:
        return Database.get_client()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client(), name)

    def table(self, *args, **kwargs):
        return self._client().table(*args, **kwargs)


# Global backend accessor (service role, bypasses RLS) created lazily so API
# readiness is not delayed by importing the Supabase client dependency graph.
db = LazyDatabaseClient()

