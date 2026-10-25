"""Monitor routes for watchlist hits.

The dashboard list itself is read through the operator-gated
``list_watchlist_hits`` RPC. This router only adds the deep-link resolver the
browser cannot compute on its own: ``t.me/c/<group>/<thread>/<message>`` needs
the numeric supergroup id and the forum topic id, which live behind the
broadcaster's Telegram helpers. The resolution is reused verbatim from
:mod:`app.services.watchlist_alerts` so no group/topic logic is duplicated.
"""

from pydantic import BaseModel

from app.services.watchlist_alerts import _resolve_message_deeplink

from .common import *

router = APIRouter()


class WatchlistHitLinkOut(BaseModel):
    url: str | None = None


@router.get("/watchlist/hits/{hit_id}/link", response_model=WatchlistHitLinkOut)
async def watchlist_hit_link(hit_id: UUID) -> WatchlistHitLinkOut:
    """Return the Telegram deep link for one hit, or ``null`` if unresolvable.

    A hit that does not exist is a 404. Missing configuration, a deleted
    message, or any Telegram failure degrades to ``{"url": null}`` exactly like
    the alert lane: a link is optional, the hit is not.
    """
    result = await execute_async(
        db.table("watchlist_hits")
        .select("message_id, credential_id")
        .eq("id", str(hit_id))
        .limit(1)
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        raise HTTPException(status_code=404, detail="Watchlist hit not found")

    row = rows[0]
    url = await _resolve_message_deeplink(
        message_id=row.get("message_id"),
        credential_id=row.get("credential_id"),
    )
    return WatchlistHitLinkOut(url=url)
