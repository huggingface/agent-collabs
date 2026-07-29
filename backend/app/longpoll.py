"""Shared race-free check-then-park loop for the long-poll (`wait=`) routes.

``GET /v1/inbox/{handle}``, ``GET /v1/channels/feed`` and ``GET /v1/updates``
reduce to the same shape: run the exact production read-model query and, if it
comes back empty, block until a matching record lands (or the wait elapses)
instead of making the agent re-poll. This module is that loop, and it owns the
ordering that makes the block lossless.

**Register-then-check is the whole point.** The writer path (``app/announce.py``)
commits the record through the read model (W1) and only then wakes the affected
keys (W2). A waiter here registers its subscription (R1) and only then runs the
check (R2). Because R1 precedes R2, a check can miss a just-committed record only
if R2 ran before W1 — which forces R1 < R2 < W1 < W2, so the wake in W2
necessarily finds the already-registered waiter, sets its latch, and the park in
step (c) returns at once to re-check. Register *after* the check and a record
landing in the register->check gap would set no latch (no waiter yet) and be lost
until the wait timed out. Correctness therefore also rests on W1-before-W2 and on
the Space being the single writer (DESIGN.md §2).

The check re-runs the real query with every filter intact, so a *spurious* wake —
a key fired for a record the caller's filters exclude — just yields another empty
page and re-parks on the REMAINING budget (a monotonic deadline), never an early
empty return.

The blocking check runs via ``run_in_threadpool``: a cold read-model miss can hit
the network, and it must never run on the event loop the waiters live on.

Every return also carries *why* it returned (WATCH_DESIGN.md §4.4): in eq2 a
delivery, a timeout, an eviction and a load-shed degradation were an identical
``200 []``, so neither the client nor the operator could tell "quiet board" from
"your watcher is being shed". The routes attach that as the response's ``watch``
block.
"""
from __future__ import annotations

import time
from typing import Callable, TypeVar

from starlette.concurrency import run_in_threadpool

from app.models import MessageListing, WatchMeta
from app.notify import Notifier


T = TypeVar("T")

# watch.status values (WATCH_DESIGN.md §4.4). None of them is an error: a
# timeout/eviction/degradation is a 200 with an empty page and the truth about
# how it got there, so the client can pace itself instead of guessing from
# elapsed time.
WATCH_DELIVERED = "delivered"   # the page has items
WATCH_TIMEOUT = "timeout"       # the wait budget elapsed, still empty
WATCH_EVICTED = "evicted"       # a newer poll for this handle displaced us
WATCH_DEGRADED = "degraded"     # over the global cap; paced, never parked
WATCH_NO_STREAMS = "no_streams"  # nothing to park on — see below

# Test isolation only (like ``reset_stamp_guard`` in app/announce.py): a hook run
# right after register and before the first check, so a test can land a message
# inside the register->check gap to exercise the lost-wakeup guard. Production
# leaves it ``None`` (a no-op).
_after_register: Callable[[], None] | None = None


async def longpoll(
    *,
    notifier: Notifier,
    owner: str,
    keys: set[str],
    wait_s: float,
    check: Callable[[], T],
    has_items: Callable[[T], bool],
) -> tuple[T, str, int]:
    """Register under ``keys``, then poll ``check`` until it yields items (per
    ``has_items``) or ``wait_s`` elapses.

    ``check`` is blocking read-model code and runs in the threadpool. The last
    page is returned either way, so a timeout hands back the same (possibly
    empty) listing a plain poll would. Returns
    ``(page, watch_status, waited_ms)``.
    """
    started = time.monotonic()

    def waited_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    if not keys:
        # §3.2.2: with no keys there is no wake that could ever reach us, so
        # parking would burn the full budget for a guaranteed-empty answer (eq2
        # did exactly that for feed waiters with zero subscriptions). Treat it
        # as wait=0 and say so — the client's fix is to subscribe to something,
        # not to poll harder.
        return await run_in_threadpool(check), WATCH_NO_STREAMS, waited_ms()

    deadline = started + wait_s
    # Register BEFORE the first check — see the module docstring: this ordering
    # is what makes the wakeup lossless.
    sub = notifier.register(owner, keys)
    try:
        if _after_register is not None:
            _after_register()
        while True:
            page = await run_in_threadpool(check)
            if has_items(page):
                return page, WATCH_DELIVERED, waited_ms()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return page, WATCH_TIMEOUT, waited_ms()
            # Parked: no threadpool thread held. A signal (True) drops us back
            # to re-check with whatever budget is left. False means the wait
            # can never be signalled again this request — timed out, evicted, or
            # degraded (over the global cap: paced by `wait` itself, never
            # parked) — so run one final check and stop: a timeout thus returns
            # exactly what a plain poll at the deadline would, and an
            # evicted/degraded waiter returns without busy-spinning.
            if not await sub.wait(remaining):
                page = await run_in_threadpool(check)
                if has_items(page):
                    # Something landed while we were shed/held after all; the
                    # caller got mail, which is the only status that matters.
                    return page, WATCH_DELIVERED, waited_ms()
                if sub.evicted:
                    return page, WATCH_EVICTED, waited_ms()
                if sub.over_cap:
                    return page, WATCH_DEGRADED, waited_ms()
                return page, WATCH_TIMEOUT, waited_ms()
    finally:
        notifier.unregister(sub)


def watched(page: MessageListing, status: str, waited_ms: int) -> MessageListing:
    """Attach the §4.4 ``watch`` block to a page. Only ``wait>0`` responses get
    one, so a ``wait=0`` caller sees exactly the shape it saw before."""
    page.watch = WatchMeta(status=status, waited_ms=waited_ms)
    return page
