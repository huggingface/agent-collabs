"""Direct unit coverage of app/notify.py's Notifier internals.

This file is deliberately narrow. Most of the Notifier's behaviour is already
proven end-to-end through the real app by test_longpoll_api.py and
test_updates_api.py. Three behaviours, though, are subtle enough — and internal
enough — that the API-level tests only exercise them indirectly, as a side
effect of some other assertion, rather than pinning them directly:

- waking a parked waiter from a foreign OS thread, which is the exact bridge
  (``loop.call_soon_threadsafe`` on the future captured at ``register`` time)
  production relies on when the verifier's worker thread announces a verdict
  from outside the event loop;
- a wake's latch absorbing a signal that arrives before ``wait`` is ever
  called, so a wake racing a client's reconnect is never lost;
- double-wake idempotency, so two writers touching the same key in quick
  succession don't double-count or raise.

Kept here rather than folded into an API test because sinking a bug in any of
these three would surface only as an intermittent, hard-to-reproduce timing
flake three layers up (a long-poll that occasionally holds for its full
timeout instead of waking promptly) — a direct unit test turns that into a
deterministic, immediate failure at the primitive itself.
"""
from __future__ import annotations

import asyncio
import threading
import time

from app.notify import Notifier


def _notifier(per_owner: int = 4, total: int = 256) -> Notifier:
    # Mirrors production Settings defaults (config.py) for the two spread
    # knobs: a threshold of 20 keeps every wake in these tests (at most a
    # couple of subscriptions) on the instant path, never the spread-out-over-
    # wake_spread_s path meant for large broadcasts.
    return Notifier(
        max_waiters_per_owner=per_owner,
        max_waiters_total=total,
        wake_spread_s=8.0,
        wake_spread_threshold=20,
    )


def test_latch_absorbs_wake_before_wait():
    n = _notifier()

    async def scenario():
        sub = n.register("a", {"k"})
        n.wake({"k"})                     # arrives before the first wait()
        first = await sub.wait(0.01)      # consumes the latch immediately
        second = await sub.wait(0.05)     # latch cleared -> times out
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True    # not lost despite arriving between/around waits
    assert second is False


def test_double_wake_is_idempotent():
    n = _notifier()

    async def scenario():
        sub = n.register("a", {"k"})
        n.wake({"k"})
        n.wake({"k"})                 # second wake must not error or double-count
        first = await sub.wait(0.5)
        second = await sub.wait(0.05)  # only one latch was pending
        return first, second

    first, second = asyncio.run(scenario())
    assert first is True
    assert second is False


def test_wake_from_foreign_thread():
    n = _notifier()

    async def scenario():
        sub = n.register("a", {"k"})

        # Fire the wake from a plain OS thread while the coroutine is parked;
        # the bridge is loop.call_soon_threadsafe on the captured future.
        def waker():
            time.sleep(0.05)
            n.wake({"k"})

        t = threading.Thread(target=waker)
        t.start()
        result = await sub.wait(2.0)
        t.join(1.0)
        return result

    assert asyncio.run(scenario()) is True
