import httpx

from app.core.config import settings


def get_async_http_client(timeout: float | httpx.Timeout = 15.0, use_proxy: bool = True, **kwargs) -> httpx.AsyncClient:
    """Return a configured httpx.AsyncClient with optional proxy support."""
    proxy = settings.HTTP_PROXY_URL if (use_proxy and settings.HTTP_PROXY_URL) else None
    follow_redirects = kwargs.pop("follow_redirects", True)
    return httpx.AsyncClient(timeout=timeout, proxy=proxy, follow_redirects=follow_redirects, **kwargs)
