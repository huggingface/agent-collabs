from fastapi import APIRouter, Depends

from app.deps import get_notifier, get_read_model
from app.notify import Notifier
from app.read_model import ReadModel

router = APIRouter()


@router.get("/v1/healthz")
def healthz(
    notifier: Notifier = Depends(get_notifier),
    read_model: ReadModel = Depends(get_read_model),
) -> dict:
    """Liveness, plus the long-poll waiter registry's counters (WATCH_DESIGN.md
    §3.2.4). `waiters`/`owners` are live gauges; the rest are since-start
    totals. A climbing `degradations` or `evictions` is the operator's only
    warning that watchers are being served a worse contract than they asked
    for.

    `warm` is false until the startup warm-up has filled the read model; the
    status is 200 either way so the Space is not restarted for being cold.
    `read_model` carries its gauges, including per-folder listing errors."""
    return {
        "status": "ok",
        "warm": read_model.warm,
        "read_model": read_model.stats(),
        "longpoll": notifier.stats(),
    }
