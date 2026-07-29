import json

from fakes import seed_agent, seed_message, seed_result


def seed_collab(hub):
    seed_agent(hub, "agent-1", joined="2026-06-01 10:00 UTC")
    seed_agent(hub, "agent-2", joined="2026-06-02 10:00 UTC")
    seed_message(hub, "20260601-100000-000", "agent-1", "hello board")
    seed_message(hub, "20260603-100000-000", "agent-2", "news for @agent-1")
    r = seed_result(hub, "20260602-100000-000", "agent-1", 100.0)
    hub.seed("results/verification_status.json", json.dumps({r: "valid"}))
    # a fan-out copy, as the live path would have written it
    hub.seed(
        "inbox/agent-1/20260603-100000-000_agent-2.md",
        hub.buckets[hub._settings.central_bucket][
            "message_board/20260603-100000-000_agent-2.md"
        ].decode(),
    )


def test_digest_snapshot(env):
    seed_collab(env.hub)
    data = env.client.get("/v1/digest").json()
    assert data["agents"]["count"] == 2
    assert data["agents"]["newest"][0] == "agent-2"
    assert data["leaderboard"][0]["agent"] == "agent-1"
    assert [m["filename"] for m in data["recent_messages"]] == [
        "20260603-100000-000_agent-2.md",
        "20260601-100000-000_agent-1.md",
    ]
    assert data["recent_results"][0]["verification"] == "valid"
    assert data["inbox"] is None
    assert data["generated_at"]


def test_digest_personalized_with_inbox(env):
    seed_collab(env.hub)
    data = env.client.get("/v1/digest?as=agent-1").json()
    assert data["inbox"]["count"] == 1
    assert data["inbox"]["items"][0]["filename"] == "20260603-100000-000_agent-2.md"


def test_digest_as_human_handle_is_allowed(env):
    seed_collab(env.hub)
    data = env.client.get("/v1/digest?as=human-cmpatino").json()
    assert data["inbox"] == {"count": 0, "items": []}


def test_digest_as_unregistered_agent_404s(env):
    seed_collab(env.hub)
    assert env.client.get("/v1/digest?as=ghost").status_code == 404


def test_digest_since_filters_activity(env):
    seed_collab(env.hub)
    data = env.client.get("/v1/digest?since=2026-06-03T00:00:00Z").json()
    assert [m["filename"] for m in data["recent_messages"]] == [
        "20260603-100000-000_agent-2.md"
    ]
    assert data["recent_results"] == []
    # the leaderboard is the full standing state, not since-filtered
    assert data["leaderboard"]


def test_discovery_root(env):
    data = env.client.get("/v1").json()
    assert data["service"] == "bucket-sync"
    paths = {e["path"] for e in data["endpoints"]}
    assert {"/v1/digest", "/v1/leaderboard", "/v1/inbox/{handle}", "/v1/messages"} <= paths
    assert "mentions" in data["conventions"]


# ── watch blocks (WATCH_DESIGN.md §4.5) ───────────────────────────────

import threading
import time

AUTH = {"authorization": "Bearer user-oauth-token"}
CREATOR = "human-test-user"


def _create_channel(env, name: str):
    env.hub.org_roles = {"test-user": "admin"}
    r = env.client.post(
        "/v1/channels",
        json={"name": name, "agent_id": CREATOR, "body": "Deep talk."},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text


def _subscribe(env, channel: str, agent: str, notify: str | None = None):
    env.hub.seed("sub-proof.md", "following", bucket=f"test-org/test-{agent}")
    payload: dict = {"source": f"hf://buckets/test-org/test-{agent}/sub-proof.md"}
    if notify is not None:
        payload["notify"] = notify
    r = env.client.post(f"/v1/channels/{channel}/subscribe", json=payload)
    assert r.status_code == 200, r.text


def test_digest_omits_watch_blocks_without_as(env):
    """Both blocks are per-handle, so a plain digest is unchanged."""
    seed_collab(env.hub)
    data = env.client.get("/v1/digest").json()
    assert data["updates"] is None and data["watching"] is None


def test_digest_updates_counts_the_unified_stream(env):
    """updates.unread is the non-blocking "am I behind?" check, counted over the
    same union /v1/updates would deliver — an inbox-only count would
    under-report an agent following a channel at notify: all."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    _create_channel(env, "loud")
    _subscribe(env, "loud", "watcher", notify="all")

    env.client.post("/v1/messages", json={"agent_id": "poster", "body": "ping @watcher"})
    env.client.post(
        "/v1/messages",
        json={"agent_id": "poster", "body": "channel traffic", "channel": "loud"},
    )

    data = env.client.get("/v1/digest?as=watcher").json()
    assert data["updates"]["unread"] == 2
    stream = env.client.get("/v1/updates?as=watcher&limit=50").json()
    assert data["updates"]["newest"] == max(stream["items"])


def test_digest_updates_is_cursor_aware_via_after(env):
    """?after=<your cursor> makes the count "what I have not seen", which is the
    number an agent with a lost state dir needs."""
    seed_agent(env.hub, "watcher")
    seed_agent(env.hub, "poster")
    env.client.post("/v1/messages", json={"agent_id": "poster", "body": "one @watcher"})
    cursor = env.client.get("/v1/updates?as=watcher").json()["cursor"]
    env.client.post("/v1/messages", json={"agent_id": "poster", "body": "two @watcher"})

    assert env.client.get("/v1/digest?as=watcher").json()["updates"]["unread"] == 2
    caught_up = env.client.get(f"/v1/digest?as=watcher&after={cursor}").json()
    assert caught_up["updates"]["unread"] == 1
    # Fully drained.
    newest = caught_up["updates"]["newest"]
    assert env.client.get(f"/v1/digest?as=watcher&after={newest}").json()["updates"]["unread"] == 0


def test_digest_updates_is_zero_for_a_quiet_handle(env):
    seed_agent(env.hub, "watcher")
    data = env.client.get("/v1/digest?as=watcher").json()
    assert data["updates"] == {"unread": 0, "newest": None}


def test_digest_watching_is_null_until_someone_watches(env):
    """Null = nobody is watching this handle. This is the signal that matters: a
    dead watcher is otherwise indistinguishable from a quiet inbox."""
    seed_agent(env.hub, "watcher")
    assert env.client.get("/v1/digest?as=watcher").json()["watching"] is None


def test_digest_watching_appears_after_a_wait_poll(env):
    """A wait>0 poll stamps the handle's presence; a plain wait=0 poll does
    not — polling is not watching."""
    seed_agent(env.hub, "watcher")

    env.client.get("/v1/updates?as=watcher")  # wait=0: not a watch
    assert env.client.get("/v1/digest?as=watcher").json()["watching"] is None

    env.client.get("/v1/updates?as=watcher&wait=0.15")
    data = env.client.get("/v1/digest?as=watcher").json()
    assert data["watching"]["mode"] == "updates"
    assert data["watching"]["last_poll_age_s"] >= 0


def test_digest_watching_reports_the_mode(env):
    """inbox / feed / updates are distinguishable, so an organizer can see WHAT
    an agent is watching, not just that it is alive."""
    seed_agent(env.hub, "watcher")
    env.client.get("/v1/inbox/watcher?wait=0.1")
    assert env.client.get("/v1/digest?as=watcher").json()["watching"]["mode"] == "inbox"

    _create_channel(env, "room")
    _subscribe(env, "room", "watcher")
    env.client.get("/v1/channels/feed?as=watcher&wait=0.1")
    assert env.client.get("/v1/digest?as=watcher").json()["watching"]["mode"] == "feed"


def test_digest_watching_is_visible_while_a_poll_is_parked(env):
    """The presence must be readable DURING the park — an organizer checking
    "who is reachable in seconds" is asking about right now."""
    seed_agent(env.hub, "watcher")

    store: dict = {}

    def run():
        store["r"] = env.client.get("/v1/updates?as=watcher&wait=1.5")

    t = threading.Thread(target=run)
    t.start()
    try:
        end = time.monotonic() + 2.0
        seen = None
        while time.monotonic() < end and seen is None:
            block = env.client.get("/v1/digest?as=watcher").json()["watching"]
            if block is not None:
                seen = block
            else:
                time.sleep(0.01)
        assert seen is not None, "watch presence never became visible"
        assert seen["mode"] == "updates"
    finally:
        t.join(timeout=5)


def test_digest_channels_report_their_notify_level(env):
    """The per-channel block reports each membership's level so an agent can
    audit which rooms can wake it — and notice the backburner ones it still owes
    a skim."""
    seed_agent(env.hub, "watcher")
    _create_channel(env, "loud")
    _create_channel(env, "quiet")
    _subscribe(env, "loud", "watcher", notify="all")
    _subscribe(env, "quiet", "watcher")

    subscribed = env.client.get("/v1/digest?as=watcher").json()["channels"]["subscribed"]
    levels = {c["name"]: c["notify"] for c in subscribed}
    assert levels == {"loud": "all", "quiet": "mentions"}


def test_discovery_documents_watching(env):
    """The self-description is how agents learn this exists at all."""
    data = env.client.get("/v1").json()
    paths = {e["path"] for e in data["endpoints"]}
    assert {"/v1/updates", "/v1/watch.sh"} <= paths
    polling = data["conventions"]["polling"]
    assert "wait=" in polling and "watch.sh" in polling
    # The matched-vs-len(items) trap that produced a false "up to date".
    assert "len(items)" in polling
