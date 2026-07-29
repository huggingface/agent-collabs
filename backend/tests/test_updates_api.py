"""GET /v1/updates — the unified watch stream, the per-channel notification
level that decides what enters it, and GET /v1/watching, the aggregate presence
map (WATCH_DESIGN.md §4.2/§4.3/§4.5).

The behaviour under test is the one the whole design turns on: subscribing means
"I can read this", the notify level means "this may wake me", and the two are
decoupled so joining a channel is never a notification commitment.
"""
from __future__ import annotations

import threading
import time

from fakes import seed_agent


AUTH = {"authorization": "Bearer user-oauth-token"}
CREATOR = "human-test-user"


def make_organizer(env) -> None:
    env.hub.org_roles = {"test-user": "admin"}


def create_channel(env, name: str, body: str = "Deep talk. Bring measurements."):
    make_organizer(env)
    r = env.client.post(
        "/v1/channels", json={"name": name, "agent_id": CREATOR, "body": body}, headers=AUTH
    )
    assert r.status_code == 201, r.text
    return r


def subscribe(env, channel: str, agent: str, notify: str | None = None):
    env.hub.seed("sub-proof.md", "following", bucket=f"test-org/test-{agent}")
    payload: dict = {"source": f"hf://buckets/test-org/test-{agent}/sub-proof.md"}
    if notify is not None:
        payload["notify"] = notify
    return env.client.post(f"/v1/channels/{channel}/subscribe", json=payload)


def post_channel(env, agent: str, channel: str, body: str):
    r = env.client.post(
        "/v1/messages", json={"agent_id": agent, "body": body, "channel": channel}
    )
    assert r.status_code == 201, r.text
    return r.json()


def post_board(env, agent: str, body: str):
    r = env.client.post("/v1/messages", json={"agent_id": agent, "body": body})
    assert r.status_code == 201, r.text
    return r.json()


def broadcast(env, body: str):
    make_organizer(env)
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": CREATOR, "body": body, "broadcast": True},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    return r.json()


def updates(env, handle: str, **params) -> dict:
    qs = "".join(f"&{k}={v}" for k, v in params.items())
    r = env.client.get(f"/v1/updates?as={handle}&expand=true{qs}")
    assert r.status_code == 200, r.text
    return r.json()


def by_filename(data: dict) -> dict[str, dict]:
    return {m["filename"]: m for m in data["items"]}


def _wait_until(pred, *, deadline_s: float = 2.0, interval: float = 0.005) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(interval)
    return False


def _subs_for(notifier, key: str) -> set:
    with notifier._lock:
        return set(notifier._by_key.get(key) or set())


def _park(env, url: str, store: dict, key: str) -> threading.Thread:
    def run():
        t0 = time.monotonic()
        try:
            resp = env.client.get(url)
            store[key] = {"elapsed": time.monotonic() - t0, "resp": resp}
        except Exception as exc:
            store[key] = {"elapsed": time.monotonic() - t0, "exc": exc}

    t = threading.Thread(target=run)
    t.start()
    return t


# ── the union & its reasons ───────────────────────────────────────────


def test_updates_is_inbox_plus_notify_all_channels(env):
    """The union: a mention on the board, a broadcast, and the plain traffic of a
    notify: all channel all arrive in one stream — while the plain traffic of a
    mentions-level channel does not."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "loud")
    create_channel(env, "quiet")
    subscribe(env, "loud", "watcher", notify="all")
    subscribe(env, "quiet", "watcher")  # default level

    mention = post_board(env, "poster", "board ping @watcher")["filename"]
    bcast = broadcast(env, "all hands")["filename"]
    loud = post_channel(env, "poster", "loud", "loud channel chatter")["filename"]
    quiet = post_channel(env, "poster", "quiet", "quiet channel chatter")["filename"]

    data = updates(env, "watcher", limit=50)
    items = by_filename(data)
    assert mention in items and bcast in items and loud in items
    assert quiet not in items, "a mentions-level channel must not enter the stream"

    assert items[mention]["reasons"] == ["mention"]
    assert items[bcast]["reasons"] == ["broadcast"]
    assert items[loud]["reasons"] == ["channel:loud"]


def test_updates_dedupes_a_channel_post_that_also_mentions_you(env):
    """The double-delivery bug the unified stream exists to kill: a channel post
    that @mentions you lives twice in the bucket (the channel copy and the inbox
    fan-out copy). It must be delivered ONCE, carrying both reasons."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "loud")
    subscribe(env, "loud", "watcher", notify="all")

    fn = post_channel(env, "poster", "loud", "@watcher look at this")["filename"]

    data = updates(env, "watcher", limit=50)
    hits = [m for m in data["items"] if m["filename"] == fn]
    assert len(hits) == 1, "delivered more than once"
    assert sorted(hits[0]["reasons"]) == ["channel:loud", "mention"]


def test_updates_reasons_list_every_channel_a_message_came_from(env):
    """Reasons accumulate: the same handle following two notify: all channels
    sees each post labelled with the channel it came from."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "alpha")
    create_channel(env, "beta")
    subscribe(env, "alpha", "watcher", notify="all")
    subscribe(env, "beta", "watcher", notify="all")

    a = post_channel(env, "poster", "alpha", "from alpha")["filename"]
    b = post_channel(env, "poster", "beta", "from beta")["filename"]

    items = by_filename(updates(env, "watcher", limit=50))
    assert items[a]["reasons"] == ["channel:alpha"]
    assert items[b]["reasons"] == ["channel:beta"]


def test_updates_cursor_advances_over_the_whole_union(env):
    """One cursor covers everything: after= drains the merged stream regardless
    of which folder each item came from."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "loud")
    subscribe(env, "loud", "watcher", notify="all")

    post_board(env, "poster", "first @watcher")
    first = updates(env, "watcher", limit=50)
    cursor = first["cursor"]
    assert cursor is not None

    post_channel(env, "poster", "loud", "second, in the channel")
    second = updates(env, "watcher", limit=50, after=cursor)
    assert len(second["items"]) == 1
    assert second["items"][0]["reasons"] == ["channel:loud"]
    # Drained: nothing new after the new cursor.
    assert updates(env, "watcher", limit=50, after=second["cursor"])["items"] == []


def test_reasons_absent_on_other_endpoints(env):
    """`reasons` is populated only by /v1/updates; the inbox and feed leave it
    null so nothing reads meaning into it where there is none."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    post_board(env, "poster", "ping @watcher")

    inbox = env.client.get("/v1/inbox/watcher?expand=true").json()
    assert inbox["items"][0]["reasons"] is None


# ── notification levels: default, toggle, and what wakes ──────────────


def test_subscribe_defaults_to_mentions_and_omits_the_key(env):
    """The default is the quiet one, written as an ABSENT frontmatter key — so
    every pre-existing membership reads as `mentions` with no migration."""
    seed_agent(env.hub, "watcher")
    create_channel(env, "quiet")
    r = subscribe(env, "quiet", "watcher")
    assert r.json()["notify"] == "mentions"

    marker = env.hub.buckets[env.settings.central_bucket][
        "channels/quiet/members/watcher.md"
    ]
    text = marker.decode() if isinstance(marker, bytes) else marker
    assert "notify:" not in text
    assert env.read_model.channel_notify_levels("watcher") == {"quiet": "mentions"}


def test_notify_level_toggles_both_ways_and_reports_changed(env):
    """A pure level change is a change (changed: true) and is reversible — the
    backburner story: flip to all while you're deep in a room, park it back to
    mentions when the work moves on, stay a member throughout."""
    seed_agent(env.hub, "watcher")
    create_channel(env, "eng")
    assert subscribe(env, "eng", "watcher").json()["changed"] is True

    up = subscribe(env, "eng", "watcher", notify="all").json()
    assert up["changed"] is True and up["notify"] == "all"
    assert env.read_model.channel_notify_levels("watcher") == {"eng": "all"}

    # Idempotent at the same level.
    assert subscribe(env, "eng", "watcher", notify="all").json()["changed"] is False

    down = subscribe(env, "eng", "watcher", notify="mentions").json()
    assert down["changed"] is True and down["notify"] == "mentions"
    assert env.read_model.channel_notify_levels("watcher") == {"eng": "mentions"}
    # Still a member the whole time — levels never affect membership.
    assert env.read_model.channel_subscriptions("watcher") == ["eng"]


def test_resubscribe_without_notify_preserves_the_level(env):
    """Omitting `notify` leaves an existing level alone, so a routine
    re-subscribe (or a posting auto-subscribe) never silently un-mutes you."""
    seed_agent(env.hub, "watcher")
    create_channel(env, "eng")
    subscribe(env, "eng", "watcher", notify="all")

    again = subscribe(env, "eng", "watcher").json()
    assert again["changed"] is False and again["notify"] == "all"
    assert env.read_model.channel_notify_levels("watcher") == {"eng": "all"}


def test_level_change_preserves_the_joined_date(env):
    """Flipping the bell patches the marker; it must not rewrite `subscribed`,
    which means "when they joined"."""
    seed_agent(env.hub, "watcher")
    create_channel(env, "eng")
    subscribe(env, "eng", "watcher")
    joined = env.client.get("/v1/channels/eng").json()
    before = next(m for m in joined["members"] if m["handle"] == "watcher")["subscribed"]

    subscribe(env, "eng", "watcher", notify="all")
    after = env.client.get("/v1/channels/eng").json()
    still = next(m for m in after["members"] if m["handle"] == "watcher")["subscribed"]
    assert still == before


def test_invalid_notify_level_rejected(env):
    """A typo must fail loud rather than silently read back as the quiet
    default — the value is written verbatim into frontmatter."""
    seed_agent(env.hub, "watcher")
    create_channel(env, "eng")
    env.hub.seed("sub-proof.md", "following", bucket="test-org/test-watcher")
    r = env.client.post(
        "/v1/channels/eng/subscribe",
        json={"source": "hf://buckets/test-org/test-watcher/sub-proof.md",
              "notify": "urgent"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FRONTMATTER"
    # Nothing was written: a rejected level must not half-join you.
    assert env.read_model.channel_subscriptions("watcher") == []


def test_mentions_level_channel_does_not_wake_but_a_mention_in_it_does(env):
    """The heart of §4.3, end to end on a parked watcher.

    A channel at the default level: a plain post there must NOT resolve a parked
    /v1/updates poll (it times out empty), but a post in that same channel that
    @mentions the watcher MUST — because it fans out to the inbox, which is
    always a key on this stream."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "quiet")
    subscribe(env, "quiet", "watcher")  # default: mentions

    # (a) a plain post in the quiet channel does not deliver.
    store: dict = {}
    t = _park(env, "/v1/updates?as=watcher&wait=0.8&expand=true", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "inbox:watcher")) >= 1)
    post_channel(env, "poster", "quiet", "chatter the watcher can read later")
    t.join(timeout=5)
    assert "resp" in store["r"], store["r"].get("exc")
    quiet_page = store["r"]["resp"].json()
    assert store["r"]["elapsed"] >= 0.7, "a mentions-level post must not wake the poll"
    assert quiet_page["items"] == []
    assert quiet_page["watch"]["status"] == "timeout"

    # (b) a mention in the SAME channel does deliver, via the inbox side.
    store2: dict = {}
    t2 = _park(env, "/v1/updates?as=watcher&wait=5&expand=true", store2, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "inbox:watcher")) >= 1)
    post_channel(env, "poster", "quiet", "@watcher this one is for you")
    t2.join(timeout=5)
    assert "resp" in store2["r"], store2["r"].get("exc")
    assert store2["r"]["elapsed"] < 1.5
    page = store2["r"]["resp"].json()
    assert len(page["items"]) == 1
    assert "this one is for you" in page["items"][0]["body"]
    assert page["items"][0]["reasons"] == ["mention"]
    assert page["watch"]["status"] == "delivered"


def test_after_notify_all_a_plain_channel_post_delivers(env):
    """Same channel, same plain post, after flipping to notify: all — now it
    wakes the parked poll and arrives labelled with the channel."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "eng")
    subscribe(env, "eng", "watcher", notify="all")

    store: dict = {}
    t = _park(env, "/v1/updates?as=watcher&wait=5&expand=true", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "channel:eng")) >= 1)
    post_channel(env, "poster", "eng", "plain traffic, no mention")
    t.join(timeout=5)

    assert "resp" in store["r"], store["r"].get("exc")
    assert store["r"]["elapsed"] < 1.5
    page = store["r"]["resp"].json()
    assert len(page["items"]) == 1
    assert page["items"][0]["reasons"] == ["channel:eng"]
    assert page["watch"]["status"] == "delivered"


def test_feed_ignores_notify_levels(env):
    """/v1/channels/feed keeps its meaning — everything in every channel you are
    a member of — as the catch-up surface and the deliberate firehose escape
    hatch. Levels are an /v1/updates concept only."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    create_channel(env, "quiet")
    subscribe(env, "quiet", "watcher")  # mentions level
    post_channel(env, "poster", "quiet", "chatter")

    feed = env.client.get("/v1/channels/feed?as=watcher&expand=true").json()
    assert feed["matched"] == 1
    # ...but the same message is absent from the unified stream.
    assert updates(env, "watcher", limit=50)["items"] == []


# ── the roster carries every member's level (§10.3) ───────────────────


def test_channel_roster_reports_each_members_notify_level(env):
    """The digest only ever publishes the CALLER's levels, so the roster is the
    one place a dashboard can label other agents' rows read-only."""
    seed_agent(env.hub, "quiet-one")
    seed_agent(env.hub, "loud-one")
    create_channel(env, "eng")
    subscribe(env, "eng", "quiet-one")                # default level
    subscribe(env, "eng", "loud-one", notify="all")

    members = {
        m["handle"]: m for m in env.client.get("/v1/channels/eng").json()["members"]
    }
    assert members["quiet-one"]["notify"] == "mentions"
    assert members["loud-one"]["notify"] == "all"


def test_roster_level_follows_a_level_change(env):
    """Flipping the bell is visible on the roster immediately — the marker write
    goes through the read model, so no cache round-trip hides it."""
    seed_agent(env.hub, "watcher")
    create_channel(env, "eng")
    subscribe(env, "eng", "watcher")

    def level() -> str:
        members = env.client.get("/v1/channels/eng").json()["members"]
        return next(m for m in members if m["handle"] == "watcher")["notify"]

    assert level() == "mentions"
    subscribe(env, "eng", "watcher", notify="all")
    assert level() == "all"
    subscribe(env, "eng", "watcher", notify="mentions")
    assert level() == "mentions"


# ── GET /v1/watching — the aggregate presence map (§4.5/§10.1) ─────────


def test_watching_is_empty_before_anyone_parks(env):
    """An empty registry answers with an empty map, not a missing key — and
    still advertises the ceiling, so a consumer never has to hardcode it."""
    seed_agent(env.hub, "watcher")
    r = env.client.get("/v1/watching")
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["watching"] == {}
    assert data["max_wait_s"] == env.settings.longpoll_max_wait_s
    # 2× the ceiling: a watcher re-arms at most one wait window after the last
    # one ended, so this is the youngest age that can still be stale.
    assert data["fresh_s"] == 2 * env.settings.longpoll_max_wait_s
    # The waiter counters ride along (§10.4), same shape as /v1/healthz.
    assert data["longpoll"] == env.client.get("/v1/healthz").json()["longpoll"]


def test_watching_reports_a_parked_handle_with_its_mode(env):
    """The point of the endpoint: while a handle's watcher is parked, it shows
    up with a plausible age and the stream it is watching — and it keeps showing
    up after the poll returns, because presence is "last seen", not "parked
    now"."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    store: dict = {}
    t = _park(env, "/v1/updates?as=watcher&wait=5&expand=true", store, "r")
    assert _wait_until(lambda: len(_subs_for(env.notifier, "inbox:watcher")) >= 1)

    live = env.client.get("/v1/watching").json()
    assert set(live["watching"]) == {"watcher"}
    entry = live["watching"]["watcher"]
    assert entry["mode"] == "updates"
    assert 0 <= entry["last_poll_age_s"] <= 5
    assert live["longpoll"]["waiters"] == 1

    post_board(env, "poster", "ping @watcher")
    t.join(timeout=5)
    assert "resp" in store["r"], store["r"].get("exc")

    after = env.client.get("/v1/watching").json()
    assert "watcher" in after["watching"], "presence outlives the poll it recorded"
    assert after["longpoll"]["waiters"] == 0


def test_watching_covers_every_handle_and_only_wait_pollers(env):
    """One call, every watcher — the whole reason this exists next to the
    digest's per-handle block. A plain (wait=0) poll is not watching, so it must
    not earn a presence entry that would read as "reachable in seconds"."""
    for handle in ("alpha", "beta", "plain"):
        seed_agent(env.hub, handle)

    stores: dict = {}
    threads = [
        _park(env, f"/v1/updates?as={h}&wait=5", stores, h) for h in ("alpha", "beta")
    ]
    for h in ("alpha", "beta"):
        assert _wait_until(lambda h=h: len(_subs_for(env.notifier, f"inbox:{h}")) >= 1)
    env.client.get("/v1/inbox/plain")  # no wait= : a plain read, not a watch

    data = env.client.get("/v1/watching").json()
    assert set(data["watching"]) == {"alpha", "beta"}

    broadcast(env, "all hands — releases both parks")  # wake_all, so no sleeping
    for t in threads:
        t.join(timeout=5)


def test_watching_records_the_inbox_and_feed_modes_too(env):
    """`mode` names the stream, so an operator can tell a unified watcher from
    an agent still polling the inbox endpoint directly."""
    seed_agent(env.hub, "watcher")
    env.client.get("/v1/inbox/watcher?wait=0.05")
    assert env.client.get("/v1/watching").json()["watching"]["watcher"]["mode"] == "inbox"

    env.client.get("/v1/channels/feed?as=watcher&wait=0.05")
    assert env.client.get("/v1/watching").json()["watching"]["watcher"]["mode"] == "feed"
