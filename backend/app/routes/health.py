from fastapi import APIRouter, Depends

from app.deps import get_notifier
from app.notify import Notifier

router = APIRouter()


@router.get("/v1/healthz")
def healthz(notifier: Notifier = Depends(get_notifier)) -> dict:
    """Liveness, plus the long-poll waiter registry's counters (WATCH_DESIGN.md
    §3.2.4). `waiters`/`owners` are live gauges; the rest are since-start
    totals. A climbing `degradations` or `evictions` is the operator's only
    warning that watchers are being served a worse contract than they asked
    for."""
    return {"status": "ok", "longpoll": notifier.stats()}
