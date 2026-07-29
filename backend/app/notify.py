"""Keyed wakeup registry for the long-poll (`wait=`) endpoints.

A pure in-process primitive: it knows nothing about messages, only about
*keys* (plain strings such as ``inbox:{handle}`` / ``channel:{name}``). A
waiter parks on a set of keys; a writer that has just committed a record wakes
every waiter registered under the affected keys. All state here is cache —
restart-safe by loss (DESIGN.md §1); a lost wakeup only reverts an agent to the
plain poll it would have run anyway.

Two thread contexts touch this registry and the single ``threading.Lock``
serialises them:

- **Waiters** live on the event loop (async routes call ``register`` /
  ``Subscription.wait`` / ``unregister``). ``register`` captures the running
  loop so foreign threads can post work back to it.
- **Wakers** run in Starlette's threadpool — the message-write path is a sync
  route. ``wake`` therefore cannot touch a ``Future`` directly (futures are not
  thread-safe); it sets a latch under the lock and bridges to the loop with
  ``loop.call_soon_threadsafe`` on the future captured at register time.

Correctness rests on the *latch-first* rule: a wake sets ``_latch`` under the
lock and only then resolves the current future (if the waiter is parked);
``wait`` clears the latch under the lock before it parks. So a wake landing in
the gap between two ``wait`` calls, or before the first, is absorbed by the
latch and the next ``wait`` returns immediately — no wakeup is lost. Extra
wakes (double wake, or a wake racing the wait's own timeout) are idempotent:
the future check re-tests ``done()`` and the latch just stays set.

The lock is held only for O(waiters) bookkeeping and never across an ``await``
or a ``call_soon_threadsafe`` (those happen after the ``with`` block).

The registry is also the only party that knows whether *anyone* is watching a
handle, so it keeps a per-owner ``last_poll`` stamp for the digest's
``watching`` block (WATCH_DESIGN.md §4.5) and counters for ``/v1/healthz``.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Callable, Iterable


log = logging.getLogger(__name__)

# WATCH_DESIGN.md §3.2.1: an over-cap request is held for a jittered span drawn
# from this range before its one final check. Not a config knob — it is a
# pacing floor, and the only value that matters is that it is >> the ~2s a
# hot-looping client would use and << the wait ceiling.
_DEGRADED_HOLD_S = (5.0, 15.0)


class Subscription:
    """A single parked waiter, re-armable across many ``wait`` calls.

    Two flavours never enter the registry's maps and can therefore never be
    signalled — ``wait`` reports the reason instead:

    - **over cap** — handed out when the global cap is reached. It paces itself
      (see ``wait``) rather than answering instantly.
    - **evicted** — the owner's oldest, detached because a newer connection for
      the same handle arrived. It returns at once (as-if-timed-out) so an
      abandoned long-poll self-heals without the newest waiter waiting on it.

    ``unregister`` on either is a no-op.
    """

    def __init__(
        self,
        lock: threading.Lock,
        owner: str,
        keys: frozenset[str],
        loop: asyncio.AbstractEventLoop,
        *,
        over_cap: bool,
    ):
        self.owner = owner
        self.keys = keys
        self._lock = lock            # shared with the owning Notifier
        self._loop = loop            # captured at register time (event loop)
        self._over_cap = over_cap    # past the global cap; paces, never parks
        self._evicted = False        # displaced by a newer waiter for this owner
        self._degraded = over_cap    # can never be signalled again
        self._active = not over_cap  # tracked in the registry's maps
        self._latch = False          # a wake landed; the next wait consumes it
        self._future: asyncio.Future | None = None  # set only while parked

    @property
    def over_cap(self) -> bool:
        return self._over_cap

    @property
    def evicted(self) -> bool:
        with self._lock:
            return self._evicted

    async def wait(self, timeout: float) -> bool:
        """Await a signal. ``True`` = signalled, ``False`` = timed out, evicted,
        or degraded. Consumes a pending latch immediately; ``timeout <= 0``
        never parks and just reports the current latch state.

        An over-cap subscription is never in the registry, so no wake can ever
        reach it — but it does NOT return instantly. eq2 did, and its degraded
        clients hot-looped at ~2s, so degradation *increased* load exactly when
        the server was full. Instead the request is held for a jittered
        ``min(timeout, U(5, 15))``s with no registry entry (§3.2.1): one
        degraded client then costs ~1 req/10s at ≤15s delivery latency, and the
        jitter keeps a crowd of them from re-polling in lockstep.
        """
        hold: float | None = None
        with self._lock:
            if self._over_cap:
                hold = max(0.0, min(timeout, random.uniform(*_DEGRADED_HOLD_S)))
            elif self._degraded:
                return False
            elif self._latch:
                self._latch = False
                return True
            elif timeout <= 0:
                return False
            else:
                # Park on a fresh future the registry (and thus wake) can find.
                fut = self._loop.create_future()
                self._future = fut
        if hold is not None:
            # Paced, not parked: no slot held, no wake possible, no re-check
            # loop — the caller runs its one final check when we return.
            if hold > 0:
                await asyncio.sleep(hold)
            return False
        try:
            await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # wait_for cancelled `fut`; a wake that raced the timeout still set
            # the latch, so the re-check below reports it rather than losing it.
            pass
        finally:
            with self._lock:
                self._future = None
        with self._lock:
            if self._degraded:
                return False
            if self._latch:
                self._latch = False
                return True
            return False


class Notifier:
    """Registry of subscriptions keyed by string, with per-owner and global
    caps supplied at construction (like the other in-memory singletons)."""

    def __init__(
        self,
        *,
        max_waiters_per_owner: int,
        max_waiters_total: int,
        wake_spread_s: float,
        wake_spread_threshold: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._lock = threading.Lock()
        self._by_key: dict[str, set[Subscription]] = {}
        # Per owner, oldest-first, to evict the stalest connection first.
        self._by_owner: dict[str, list[Subscription]] = {}
        self._total = 0
        self._max_per_owner = max_waiters_per_owner
        self._max_total = max_waiters_total
        # A large wake spreads its future resolutions over [0, wake_spread_s]
        # so agents don't all re-poll in the same tick; see _flush. Wakes at or
        # below wake_spread_threshold targets stay instant.
        self._wake_spread_s = wake_spread_s
        self._wake_spread_threshold = wake_spread_threshold
        self._clock = clock
        # owner -> (monotonic stamp, mode) of its most recent wait>0 poll. A
        # hint for the digest's `watching` block, not an audit log: it is lost
        # on restart, and a restart reads as "nobody is watching" — the
        # truthful answer, since every parked connection died with it.
        self._last_poll: dict[str, tuple[float, str]] = {}
        # Cheap operational counters for /v1/healthz. eq2 shipped this feature
        # with zero observability, so an operator could not tell a quiet board
        # from a registry that had been degrading every request for hours.
        self._parks = 0
        self._wakes = 0
        self._evictions = 0
        self._degradations = 0

    def register(self, owner: str, keys: set[str]) -> Subscription:
        """Register a waiter under every key in ``keys``; ``owner`` is the
        polling handle, used only for cap accounting. Must be called from the
        event loop — the running loop is captured for foreign-thread wakes.

        Per-owner cap exceeded -> evict this owner's OLDEST subscription (its
        ``wait`` returns ``False`` as if timed out, self-healing an abandoned
        long-poll so the newest connection is the live one). Global cap
        exceeded -> return an over-cap, untracked subscription that paces
        itself (the endpoint falls back to a slowed plain poll rather than
        erroring under load).
        """
        loop = asyncio.get_running_loop()
        keyset = frozenset(keys)
        evicted: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        with self._lock:
            owned = self._by_owner.get(owner)
            while owned is not None and len(owned) >= self._max_per_owner:
                oldest = owned[0]
                oldest._degraded = True  # its parked wait() will return False
                oldest._evicted = True   # ...and say why, for watch.status
                fut = self._detach_locked(oldest)
                if fut is not None:
                    evicted.append((oldest._loop, fut))
                self._evictions += 1
                owned = self._by_owner.get(owner)  # re-fetch; None once emptied
            over_cap = self._total >= self._max_total
            sub = Subscription(self._lock, owner, keyset, loop, over_cap=over_cap)
            if over_cap:
                self._degradations += 1
            else:
                self._by_owner.setdefault(owner, []).append(sub)
                for key in keyset:
                    self._by_key.setdefault(key, set()).add(sub)
                self._total += 1
                self._parks += 1
            live = self._total
        # Logged outside the lock; both lines are the operator's only warning
        # that watchers are being served a worse contract than they asked for.
        if evicted:
            log.info(
                "longpoll: evicted %d stale waiter(s) for owner=%s (per-owner cap %d)",
                len(evicted), owner, self._max_per_owner,
            )
        if over_cap:
            log.warning(
                "longpoll: global waiter cap reached (%d/%d) — owner=%s degraded to a "
                "paced poll (no registry slot, held ~%.0f-%.0fs)",
                live, self._max_total, owner, *_DEGRADED_HOLD_S,
            )
        self._flush(evicted)
        return sub

    def unregister(self, sub: Subscription) -> None:
        """Remove a subscription from the registry. Idempotent, and a no-op for
        over-cap/evicted subscriptions (never a KeyError)."""
        with self._lock:
            self._detach_locked(sub)

    def wake(self, keys: Iterable[str]) -> int:
        """Signal every subscription registered under any of ``keys``. Thread-
        safe: safe to call from the threadpool while waiters live on the loop.
        Returns the number of subscriptions signalled."""
        with self._lock:
            targets: set[Subscription] = set()
            for key in keys:
                bucket = self._by_key.get(key)
                if bucket:
                    targets.update(bucket)
            pending = self._arm_locked(targets)
            self._wakes += len(targets)
        self._flush(pending, spread_s=self._wake_spread_s)
        return len(targets)

    def wake_all(self) -> int:
        """Broadcast: signal every registered subscription. Returns the count."""
        with self._lock:
            targets: set[Subscription] = set()
            for bucket in self._by_key.values():
                targets.update(bucket)
            pending = self._arm_locked(targets)
            self._wakes += len(targets)
        self._flush(pending, spread_s=self._wake_spread_s)
        return len(targets)

    # ── liveness & observability ──

    def note_poll(self, owner: str, mode: str) -> None:
        """Record that ``owner`` just opened a ``wait>0`` poll in ``mode``
        (updates|inbox|feed). The server side of "is anyone watching this
        handle?" — the one liveness signal that survives total client amnesia
        (WATCH_DESIGN.md §4.5/§6)."""
        with self._lock:
            self._last_poll[owner] = (self._clock(), mode)

    def last_poll(self, owner: str) -> tuple[float, str] | None:
        """(age in seconds, mode) of ``owner``'s most recent ``wait>0`` poll, or
        ``None`` if this process has never seen one."""
        with self._lock:
            seen = self._last_poll.get(owner)
            if seen is None:
                return None
            return max(0.0, self._clock() - seen[0]), seen[1]

    def all_last_poll(self) -> dict[str, tuple[float, str]]:
        """``{owner: (age in seconds, mode)}`` for every handle this process has
        ever served a ``wait>0`` poll for — the whole presence map in ONE lock
        acquisition, for ``GET /v1/watching``.

        The aggregate exists because the per-handle answer is the wrong shape for
        the only consumer that wants all of them: a dashboard drawing a dot per
        agent would otherwise have to ask for one full digest per registered
        handle every poll, computing inbox records, channel summaries and a
        leaderboard N times over to read N entries out of this dict. Same hint
        semantics as ``last_poll``: an absent handle means nobody is watching it.
        """
        with self._lock:
            now = self._clock()
            return {
                owner: (max(0.0, now - stamp), mode)
                for owner, (stamp, mode) in self._last_poll.items()
            }

    def stats(self) -> dict[str, int]:
        """Counters + the live waiter gauge, for /v1/healthz."""
        with self._lock:
            return {
                "waiters": self._total,
                "owners": len(self._by_owner),
                "parks": self._parks,
                "wakes": self._wakes,
                "evictions": self._evictions,
                "degradations": self._degradations,
            }

    # ── internals (all _locked helpers require self._lock held) ──

    def _arm_locked(
        self, targets: set[Subscription]
    ) -> list[tuple[asyncio.AbstractEventLoop, asyncio.Future]]:
        pending: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        for sub in targets:
            sub._latch = True  # set BEFORE resolving so wait can't miss it
            fut = sub._future
            if fut is not None and not fut.done():
                pending.append((sub._loop, fut))
        return pending

    def _detach_locked(self, sub: Subscription) -> asyncio.Future | None:
        """Drop ``sub`` from every map. Returns its live parked future (if any)
        so the caller can resolve it after releasing the lock; ``None`` if the
        sub was already inactive (over-cap/evicted/unregistered)."""
        if not sub._active:
            return None
        sub._active = False
        for key in sub.keys:
            bucket = self._by_key.get(key)
            if bucket is not None:
                bucket.discard(sub)
                if not bucket:
                    del self._by_key[key]
        owned = self._by_owner.get(sub.owner)
        if owned is not None:
            try:
                owned.remove(sub)
            except ValueError:
                pass
            if not owned:
                del self._by_owner[sub.owner]
        self._total -= 1
        fut = sub._future
        if fut is not None and not fut.done():
            return fut
        return None

    def _flush(
        self,
        pending: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]],
        *,
        spread_s: float = 0.0,
    ) -> None:
        # Cross the thread boundary outside the lock: the loop resolves each
        # future on its own thread, where touching it is safe.
        #
        # A large wake (a broadcast, or a busy channel) would otherwise resolve
        # every parked long-poll in the same instant, so all agents re-poll at
        # once — a synchronized request spike into this Space that can trip the
        # *.hf.space edge rate limit. When more than `wake_spread_threshold`
        # waiters are woken, spread their releases uniformly over
        # [0, spread_s] so the re-polls arrive staggered. Small/targeted wakes
        # (@mentions) and eviction flushes (spread_s=0) stay instant.
        if spread_s > 0.0 and len(pending) > self._wake_spread_threshold:
            for loop, fut in pending:
                offset = random.uniform(0.0, spread_s)
                # call_later must run on the loop thread — hop there first.
                loop.call_soon_threadsafe(loop.call_later, offset, _resolve_future, fut)
        else:
            for loop, fut in pending:
                loop.call_soon_threadsafe(_resolve_future, fut)


def _resolve_future(fut: asyncio.Future) -> None:
    # Runs on the owning loop. Idempotent: a double wake, or a wake that raced
    # the wait's own timeout/cancel, may find the future already resolved.
    if not fut.done():
        fut.set_result(True)
