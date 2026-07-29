"""Integration tests for the long-poll (`wait=`) endpoints (WATCH_DESIGN.md §3).

Drives in-flight long-polls the only way a sync TestClient can: each parked GET
runs on its own ``threading.Thread`` (the client dispatches concurrent requests
from many threads onto its single portal loop, exactly the split the design
relies on — waiters live on the loop, sync write routes wake them from the
threadpool). "Parked" is detected deterministically by polling the notifier's
registry under its lock until the waiter appears, never by a fixed sleep, so
the wake/re-park races resolve the same way every run.
"""
from __future__ import annotations

import threading
import time

from app.announce import promote_message
from app.naming import stamp_yaml, utc_now
from fakes import seed_agent


# ── harness helpers ───────────────────────────────────────────────────

AUTH = {"authorization": "Bearer user-oauth-token"}
# Organizer actions run as the signed-in human (FakeHub whoami == "test-user").
CREATOR = "human-test-user"


def _subs_for(notifier, key: str) -> set:
    """Snapshot of the subscriptions registered under ``key`` (guarded read —
    the registry is touched from the loop and the threadpool)."""
    with notifier._lock:
        return set(notifier._by_key.get(key) or set())


def _wait_until(pred, *, deadline_s: float = 2.0, interval: float = 0.005) -> bool:
    """Poll ``pred`` until true or the deadline. Used to wait for a waiter to be
    registered — a deterministic signal, so it can't flake the way a fixed
    "give it a beat to park" sleep would."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def _park(env, url: str, store: dict, key: str) -> threading.Thread:
    """Start ``GET url`` on a worker thread; record (elapsed, response) — or the
    exception — under ``store[key]`` when it returns."""

    def run():
        t0 = time.monotonic()
        try:
            resp = env.client.get(url)
            store[key] = {"elapsed": time.monotonic() - t0, "resp": resp}
        except Exception as exc:  # surfaced by the caller's assertions
            store[key] = {"elapsed": time.monotonic() - t0, "exc": exc}

    t = threading.Thread(target=run)
    t.start()
    return t


def _make_organizer(env) -> None:
    env.hub.org_roles = {"test-user": "admin"}


def _create_channel(env, name: str, body: str = "Deep talk. Bring measurements.") -> None:
    _make_organizer(env)
    r = env.client.post(
        "/v1/channels", json={"name": name, "agent_id": CREATOR, "body": body}, headers=AUTH
    )
    assert r.status_code == 201, r.text


def _subscribe(env, channel: str, agent: str, notify: str | None = None) -> None:
    # Existence of a file in the agent's own bucket is the ownership proof.
    env.hub.seed("sub-proof.md", "following", bucket=f"test-org/test-{agent}")
    uri = f"hf://buckets/test-org/test-{agent}/sub-proof.md"
    payload: dict = {"source": uri}
    if notify is not None:
        payload["notify"] = notify
    r = env.client.post(f"/v1/channels/{channel}/subscribe", json=payload)
    assert r.status_code == 200, r.text


def _post_channel(env, agent: str, channel: str, body: str):
    return env.client.post(
        "/v1/messages", json={"agent_id": agent, "body": body, "channel": channel}
    )


def _broadcast(env, body: str = "heads up everyone"):
    _make_organizer(env)
    return env.client.post(
        "/v1/messages",
        json={"agent_id": CREATOR, "body": body, "broadcast": True},
        headers=AUTH,
    )


# ── 1. inbox wake ─────────────────────────────────────────────────────


def test_inbox_wake_on_board_mention(env):
    """A parked inbox waiter is woken well under its budget by a board message
    mentioning it, and gets exactly the new record."""
    seed_agent(env.hub, "agent-a")
    seed_agent(env.hub, "agent-b")
    store: dict = {}
    t = _park(env, "/v1/inbox/agent-a?wait=5&expand=true", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "inbox:agent-a")) >= 1)

    env.client.post("/v1/messages", json={"agent_id": "agent-b", "body": "ping @agent-a"})
    t.join(timeout=5)

    assert "resp" in store["r"], store["r"].get("exc")
    assert store["r"]["elapsed"] < 1.5  # woken, not timed out
    data = store["r"]["resp"].json()
    assert data["count"] == 1
    assert data["items"][0]["frontmatter"]["agent"] == "agent-b"
    assert "ping @agent-a" in data["items"][0]["body"]
    assert data["watch"] == {"status": "delivered", "waited_ms": data["watch"]["waited_ms"]}


# ── 2. lost-wakeup regression ─────────────────────────────────────────


def test_lost_wakeup_regression(env, monkeypatch):
    """A message landing in the register->check gap must not be lost. The
    ``_after_register`` hook lands one synchronously (calling ``promote_message``
    directly — an ``env.client.post`` here would re-enter the loop and deadlock);
    the first check must already see it and return at once, no timeout sleep."""
    seed_agent(env.hub, "agent-a")
    seed_agent(env.hub, "agent-b")
    import app.longpoll as longpoll_mod

    def hook() -> None:
        now = utc_now()
        fm = {"type": "agent", "agent": "agent-b", "timestamp": stamp_yaml(now), "via": "raw"}
        promote_message(
            settings=env.settings,
            hub=env.hub,
            read_model=env.read_model,
            agent_id="agent-b",
            fm=fm,
            body="landed in the gap @agent-a",
            now=now,
            notifier=env.notifier,
        )

    monkeypatch.setattr(longpoll_mod, "_after_register", hook)

    store: dict = {}
    t = _park(env, "/v1/inbox/agent-a?wait=5&expand=true", store, "r")
    t.join(timeout=5)

    assert "resp" in store["r"], store["r"].get("exc")
    assert store["r"]["elapsed"] < 1.0  # returned on the first check, never parked
    data = store["r"]["resp"].json()
    assert data["count"] == 1
    assert "landed in the gap" in data["items"][0]["body"]


# ── 3. filter re-park ─────────────────────────────────────────────────


def test_filter_mismatch_reparks_until_timeout(env):
    """A filtered-out arrival fires the key but the check (filters intact)
    excludes it, so the waiter re-parks on the remaining budget and times out
    empty rather than returning early."""
    seed_agent(env.hub, "agent-a")
    seed_agent(env.hub, "agent-b")
    store: dict = {}
    t = _park(env, "/v1/inbox/agent-a?wait=1&type=verification&expand=true", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "inbox:agent-a")) >= 1)

    # type: agent — wakes inbox:agent-a, but the type=verification filter drops it.
    env.client.post("/v1/messages", json={"agent_id": "agent-b", "body": "off-type @agent-a"})
    t.join(timeout=5)

    assert "resp" in store["r"], store["r"].get("exc")
    assert store["r"]["elapsed"] >= 0.9  # ran to the deadline, no early return
    data = store["r"]["resp"].json()
    # The record IS in the inbox (count reflects unfiltered records) but the
    # type filter keeps it out of the page — an empty page, so the loop re-parks.
    assert data["count"] == 1 and data["matched"] == 0 and data["items"] == []
    assert data["watch"]["status"] == "timeout"


# ── 4. broadcast ──────────────────────────────────────────────────────


def test_broadcast_wakes_inbox_not_feed(env):
    """A broadcast reaches an inbox waiter (read-time union) but is not a channel
    message: a subscribed-channel feed waiter is woken by wake_all yet re-parks
    empty on re-check and times out."""
    seed_agent(env.hub, "agent-a")  # inbox waiter
    seed_agent(env.hub, "agent-b")  # feed waiter (subscribed to c1)
    _create_channel(env, "c1")
    _subscribe(env, "c1", "agent-b")

    store: dict = {}
    ti = _park(env, "/v1/inbox/agent-a?wait=3&expand=true", store, "inbox")
    tf = _park(env, "/v1/channels/feed?as=agent-b&wait=0.8&expand=true", store, "feed")
    assert _wait_until(
        lambda: len(_subs_for(env.notifier, "inbox:agent-a")) >= 1
        and len(_subs_for(env.notifier, "channel:c1")) >= 1
    )

    _broadcast(env, body="all hands")
    ti.join(timeout=5)
    tf.join(timeout=5)

    assert "resp" in store["inbox"], store["inbox"].get("exc")
    assert store["inbox"]["elapsed"] < 1.5
    inbox = store["inbox"]["resp"].json()
    assert any("all hands" in m["body"] for m in inbox["items"])

    assert "resp" in store["feed"], store["feed"].get("exc")
    assert store["feed"]["elapsed"] >= 0.7  # timed out, not delivered
    feed = store["feed"]["resp"].json()
    assert feed["count"] == 0 and feed["items"] == []
    assert feed["watch"]["status"] == "timeout"


# ── 5. channel key ────────────────────────────────────────────────────


def test_feed_wakes_only_on_subscribed_channel(env):
    """A feed waiter parks on its subscribed channels only: a post into a channel
    it does not follow (c2) never reaches it, while a post into a followed channel
    (c1) wakes it with the record. The negative runs first, while c1 is still
    empty, so the waiter genuinely parks rather than returning an existing page."""
    seed_agent(env.hub, "agent-a")
    seed_agent(env.hub, "agent-b")
    _create_channel(env, "c1")
    _create_channel(env, "c2")
    _subscribe(env, "c1", "agent-a")  # subscribed to c1 only

    # A c2-only post does not wake a c1-subscribed waiter (keys = {channel:c1}).
    store2: dict = {}
    t2 = _park(env, "/v1/channels/feed?as=agent-a&wait=0.8&expand=true", store2, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "channel:c1")) >= 1)
    _post_channel(env, "agent-b", "c2", "c2 chatter agent-a does not follow")
    t2.join(timeout=5)

    assert "resp" in store2["r"], store2["r"].get("exc")
    assert store2["r"]["elapsed"] >= 0.7  # timed out, not woken
    data2 = store2["r"]["resp"].json()
    assert data2["count"] == 0 and data2["items"] == []

    # A post into c1 wakes agent-a's feed with the record.
    store: dict = {}
    t = _park(env, "/v1/channels/feed?as=agent-a&wait=3&expand=true", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "channel:c1")) >= 1)
    _post_channel(env, "agent-b", "c1", "c1 finding for the feed")
    t.join(timeout=5)

    assert "resp" in store["r"], store["r"].get("exc")
    assert store["r"]["elapsed"] < 1.5
    assert any("c1 finding" in m["body"] for m in store["r"]["resp"].json()["items"])


# ── 6. per-owner eviction & global-cap degrade at the API level ───────


def test_per_owner_eviction_evicts_oldest_newest_delivers(make_env):
    """With a per-owner cap of 1, registering a second waiter for the same handle
    evicts the first from the registry; the evicted waiter returns an empty
    (as-if-timeout) page while the newest, live waiter still delivers on a post."""
    env = make_env(LONGPOLL_MAX_WAITERS_PER_OWNER=1)
    seed_agent(env.hub, "agent-a")
    seed_agent(env.hub, "agent-b")
    key = "inbox:agent-a"
    store: dict = {}

    t1 = _park(env, "/v1/inbox/agent-a?wait=0.5&expand=true", store, "w1")
    assert _wait_until(lambda: len(_subs_for(env.notifier, key)) >= 1)
    sub1 = next(iter(_subs_for(env.notifier, key)))

    # Waiter 2 registering evicts the oldest (sub1) — a deterministic registry
    # swap, independent of any wake.
    t2 = _park(env, "/v1/inbox/agent-a?wait=3&expand=true", store, "w2")
    assert _wait_until(
        lambda: sub1 not in _subs_for(env.notifier, key)
        and len(_subs_for(env.notifier, key)) >= 1
    )
    assert len(_subs_for(env.notifier, key)) == 1  # only the newest remains tracked

    # Waiter 1 (no message during its window) comes back empty, and says WHY:
    # "evicted" is what tells a client it was displaced rather than that the
    # board is quiet — in eq2 both were an identical 200 [].
    t1.join(timeout=5)
    assert "resp" in store["w1"], store["w1"].get("exc")
    w1 = store["w1"]["resp"].json()
    assert w1["count"] == 0
    assert w1["watch"]["status"] == "evicted"

    # The live (newest) waiter is still wakeable and delivers.
    env.client.post("/v1/messages", json={"agent_id": "agent-b", "body": "for the live one @agent-a"})
    t2.join(timeout=5)
    assert "resp" in store["w2"], store["w2"].get("exc")
    w2 = store["w2"]["resp"].json()
    assert w2["count"] == 1 and "for the live one" in w2["items"][0]["body"]


def test_evicted_waiter_returns_promptly(make_env):
    """An evicted waiter's wait() returns as-if-timed-out, so the request
    returns at once (one final check) rather than spinning to the deadline. It
    must NOT be paced like a degraded one — eviction means a newer connection is
    already serving this handle, so there is nothing to slow down for."""
    env = make_env(LONGPOLL_MAX_WAITERS_PER_OWNER=1)
    seed_agent(env.hub, "agent-a")
    key = "inbox:agent-a"
    store: dict = {}

    t1 = _park(env, "/v1/inbox/agent-a?wait=0.5&expand=true", store, "w1")
    assert _wait_until(lambda: len(_subs_for(env.notifier, key)) >= 1)
    sub1 = next(iter(_subs_for(env.notifier, key)))
    t2 = _park(env, "/v1/inbox/agent-a?wait=0.5&expand=true", store, "w2")
    assert _wait_until(lambda: sub1 not in _subs_for(env.notifier, key))

    t1.join(timeout=5)
    t2.join(timeout=5)
    assert store["w1"]["elapsed"] < 0.2  # should be immediate; bug => ~0.5s


def test_global_cap_degrades_to_plain_poll(make_env):
    """Over the global cap the endpoint hands out a degraded subscription and
    falls back to a plain poll — same empty listing shape a wait=0 poll returns,
    never an error under load."""
    env = make_env(LONGPOLL_MAX_WAITERS_TOTAL=0)
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/inbox/agent-a?wait=0.5")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0 and data["matched"] == 0 and data["items"] == []
    assert data["watch"]["status"] == "degraded"


# ── 6b. degraded pacing (WATCH_DESIGN.md §3.2.1) ──────────────────────


def test_degraded_request_is_held_not_answered_hot(make_env):
    """The eq2 regression this fixes: over the global cap eq2 answered
    instantly-empty, so the client re-polled at ~2s and degradation *raised*
    load exactly when the server was full.

    Here an over-cap request is HELD for a jittered min(wait, U(5,15))s with no
    registry entry, then does one final check. With a 0.6s wait the min() clamp
    is what dominates, so the request must consume essentially its whole budget
    (not return instantly) while never occupying a waiter slot."""
    env = make_env(LONGPOLL_MAX_WAITERS_TOTAL=0)
    seed_agent(env.hub, "agent-a")

    t0 = time.monotonic()
    r = env.client.get("/v1/inbox/agent-a?wait=0.6")
    elapsed = time.monotonic() - t0

    assert r.status_code == 200
    data = r.json()
    assert data["watch"]["status"] == "degraded"
    # Held for the clamped hold (~the full 0.6s budget), NOT answered hot.
    assert elapsed >= 0.5, f"degraded request returned hot after {elapsed:.3f}s"
    # ...and bounded by the wait the caller asked for: the hold is
    # min(wait, U(5,15)), so it can never exceed the budget.
    assert elapsed < 2.0
    # It cost no waiter slot: nothing was ever registered.
    assert env.notifier.stats()["waiters"] == 0
    assert env.notifier.stats()["degradations"] >= 1
    assert env.notifier.stats()["parks"] == 0


def test_degraded_hold_is_bounded_by_a_tiny_wait(make_env):
    """min(wait, hold): a caller asking for a 0.1s wait still gets ~0.1s, so the
    pacing can never stretch a request past what the client budgeted."""
    env = make_env(LONGPOLL_MAX_WAITERS_TOTAL=0)
    seed_agent(env.hub, "agent-a")
    t0 = time.monotonic()
    r = env.client.get("/v1/inbox/agent-a?wait=0.1")
    elapsed = time.monotonic() - t0
    assert r.json()["watch"]["status"] == "degraded"
    assert elapsed < 1.0


# ── 6c. empty key set never parks (WATCH_DESIGN.md §3.2.2) ────────────


def test_empty_keyset_short_circuits_with_no_streams(env):
    """eq2 parked a feed waiter with zero subscriptions for the full 55s with
    zero wake possibility (wake_all only reaches waiters that hold at least one
    key, so the comment claiming broadcasts would reach it was wrong).

    Here an empty key set is treated as wait=0: immediate return, and
    watch.status says `no_streams` so the client learns its fix is to subscribe
    to something rather than to poll harder."""
    seed_agent(env.hub, "agent-a")  # a member of nothing

    t0 = time.monotonic()
    r = env.client.get("/v1/channels/feed?as=agent-a&wait=30")
    elapsed = time.monotonic() - t0

    assert r.status_code == 200
    data = r.json()
    assert data["items"] == [] and data["count"] == 0
    assert data["watch"]["status"] == "no_streams"
    assert elapsed < 1.0, f"parked for {elapsed:.2f}s on a keyless wait"
    assert env.notifier.stats()["parks"] == 0  # never registered


def test_updates_never_reports_no_streams(env):
    """/v1/updates always holds the inbox key, so it can never short-circuit —
    a timeout there is a real timeout."""
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/updates?as=agent-a&wait=0.4")
    assert r.status_code == 200
    assert r.json()["watch"]["status"] == "timeout"


# ── 7. timeout & the wait+before guard ────────────────────────────────


def test_timeout_returns_empty_listing_shape(env):
    """No writes → the waiter times out after ~wait and returns the same empty
    listing (count/matched/items) a plain poll would."""
    seed_agent(env.hub, "agent-a")
    t0 = time.monotonic()
    r = env.client.get("/v1/inbox/agent-a?wait=0.8")
    elapsed = time.monotonic() - t0

    assert r.status_code == 200
    assert 0.6 <= elapsed < 2.0  # waited roughly the budget
    data = r.json()
    assert data["count"] == 0 and data["matched"] == 0 and data["items"] == []
    assert data["watch"]["status"] == "timeout"
    assert data["watch"]["waited_ms"] >= 600


def test_inbox_wait_with_before_rejected(env):
    """`wait` + `before` is a 400 INVALID_QUERY: a before-cursor page can never
    gain items, so the wait could never resolve early."""
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/inbox/agent-a?wait=1&before=20260101-000000-000_x.md")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_QUERY"


def test_feed_wait_with_before_rejected(env):
    """The same guard on the other long-poll endpoint."""
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/channels/feed?as=agent-a&wait=1&before=20260101-000000-000_x.md")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_QUERY"


def test_updates_wait_with_before_rejected(env):
    """...and on the unified stream."""
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/updates?as=agent-a&wait=1&before=20260101-000000-000_x.md")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_QUERY"


def test_before_without_wait_still_allowed(env):
    """The guard is about `wait`, not about `before`: a plain before-page is a
    normal backward query and must keep working."""
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/inbox/agent-a?before=20260101-000000-000_x.md")
    assert r.status_code == 200


# ── 8. registration is checked before anything is registered ──────────


def test_unregistered_handle_404s_without_taking_a_waiter_slot(env):
    """An unregistered handle 404s immediately (§7) — and the registry is left
    untouched, so the waiter table can't be filled with fabricated names."""
    t0 = time.monotonic()
    r = env.client.get("/v1/inbox/ghost-agent?wait=30")
    elapsed = time.monotonic() - t0

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"
    assert elapsed < 1.0
    assert env.notifier.stats()["parks"] == 0
    assert env.notifier.stats()["waiters"] == 0
    # ...and no phantom watch presence was recorded for a name that doesn't exist.
    assert env.notifier.last_poll("ghost-agent") is None


# ── extra: wait clamp ─────────────────────────────────────────────────


def test_wait_is_clamped_to_max(make_env):
    """`wait` is clamped to LONGPOLL_MAX_WAIT_S: a huge value returns after the
    cap, nowhere near the requested seconds — clamped, never rejected."""
    env = make_env(LONGPOLL_MAX_WAIT_S=0.5)
    seed_agent(env.hub, "agent-a")
    t0 = time.monotonic()
    r = env.client.get("/v1/inbox/agent-a?wait=9999")
    elapsed = time.monotonic() - t0

    assert r.status_code == 200
    assert elapsed < 2.0  # clamped to 0.5s
    assert r.json()["count"] == 0


def test_negative_wait_is_a_plain_poll(env):
    """A negative wait clamps to 0: a plain poll, and no `watch` block at all
    (so a wait=0 caller's response shape is byte-for-byte what it was before
    this feature existed)."""
    seed_agent(env.hub, "agent-a")
    r = env.client.get("/v1/inbox/agent-a?wait=-5")
    assert r.status_code == 200
    assert r.json()["watch"] is None


# ── 9. notifier counters land on /v1/healthz (§3.2.4) ─────────────────


def test_healthz_exposes_waiter_stats(env):
    """eq2 shipped this feature with zero observability. The gauge must move
    while a waiter is actually parked, not just after the fact."""
    seed_agent(env.hub, "agent-a")
    seed_agent(env.hub, "agent-b")
    assert env.client.get("/v1/healthz").json()["longpoll"]["waiters"] == 0

    store: dict = {}
    t = _park(env, "/v1/inbox/agent-a?wait=3", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "inbox:agent-a")) >= 1)
    live = env.client.get("/v1/healthz").json()["longpoll"]
    assert live["waiters"] == 1 and live["owners"] == 1 and live["parks"] >= 1

    env.client.post("/v1/messages", json={"agent_id": "agent-b", "body": "hi @agent-a"})
    t.join(timeout=5)

    after = env.client.get("/v1/healthz").json()["longpoll"]
    assert after["waiters"] == 0  # released on return
    assert after["wakes"] >= 1
