"""Monitor API router assembled from resource routers."""

from app.api.routers._monitor import broadcasts as _broadcasts
from app.api.routers._monitor import credentials as _credentials
from app.api.routers._monitor import engagement as _engagement
from app.api.routers._monitor import exports as _exports
from app.api.routers._monitor import findings as _findings
from app.api.routers._monitor import messages as _messages
from app.api.routers._monitor import operators as _operators
from app.api.routers._monitor import ops as _ops
from app.api.routers._monitor import outposts as _outposts
from app.api.routers._monitor import search as _search
from app.api.routers._monitor import stats as _stats
from app.api.routers._monitor import targets as _targets
from app.api.routers._monitor import topics as _topics
from app.api.routers._monitor import watchlist_hits as _watchlist_hits
from app.api.routers._monitor import webhooks as _webhooks
from app.api.routers._monitor.common import APIRouter, Depends, require_monitor_key

router = APIRouter(
    prefix="/monitor",
    tags=["Monitor"],
    dependencies=[Depends(require_monitor_key)],
)
router.include_router(_stats.router)
router.include_router(_credentials.router)
router.include_router(_messages.router)
router.include_router(_findings.router)
router.include_router(_engagement.router)
router.include_router(_exports.router)
router.include_router(_broadcasts.router)
router.include_router(_topics.router)
router.include_router(_webhooks.router)
router.include_router(_targets.router)
router.include_router(_search.router)
router.include_router(_operators.router)
router.include_router(_outposts.router)
router.include_router(_ops.router)
router.include_router(_watchlist_hits.router)

# Compatibility re-exports for callers/tests that expect get_stats on this module:
get_stats = _stats.get_stats
_get_monitor_stats = _stats._get_monitor_stats
_STATS_REDIS_KEY = _stats._STATS_REDIS_KEY

