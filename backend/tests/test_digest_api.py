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


def test_discovery_limits_come_from_settings(make_env):
    env = make_env(RAW_MESSAGE_PER_HOUR=7, MESSAGE_MAX_BYTES=1234, MENTION_FANOUT_CAP=3)
    limits = env.client.get("/v1").json()["limits"]
    assert limits["raw_messages_per_hour_per_agent"] == 7
    assert limits["source_max_bytes"] == 1234
    assert limits["mention_fanout_cap"] == 3
    assert limits["raw_body_max_chars"] == 32 * 1024
    assert limits["expand_max_limit"] == env.settings.expand_max_limit


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

    assert env.client.get("/v1/digest?as=watcher&after=").json()["updates"]["unread"] == 2
    caught_up = env.client.get(f"/v1/digest?as=watcher&after={cursor}").json()
    assert caught_up["updates"]["unread"] == 1
    # Fully drained.
    newest = caught_up["updates"]["newest"]
    assert env.client.get(f"/v1/digest?as=watcher&after={newest}").json()["updates"]["unread"] == 0


def test_a_partial_or_filtered_read_does_not_acknowledge_other_mail(env):
    """The server keeps no cursor for a handle, so no read moves `unread`: a
    newest-first page of one, a wait=0 status-style read, a peek — none of them
    is a delivery, and the two messages such a page never returned must still
    count. (A server-side checkpoint moved by `page.cursor` skipped them.)"""
    seed_agent(env.hub, "reader")
    seed_agent(env.hub, "writer")
    for i in range(3):
        r = env.client.post("/v1/messages", json={"agent_id": "writer", "body": f"@reader message {i}"})
        assert r.status_code == 201, r.text
    assert env.client.get("/v1/digest?as=reader").json()["updates"]["unread"] == 3

    page = env.client.get("/v1/updates?as=reader&limit=1&expand=true").json()
    assert len(page["items"]) == 1  # newest first: the two older ones were never returned
    assert env.client.get("/v1/digest?as=reader").json()["updates"]["unread"] == 3

    page = env.client.get("/v1/updates?as=reader&order=asc&wait=0&limit=1").json()
    assert page["items"]
    assert env.client.get("/v1/digest?as=reader").json()["updates"]["unread"] == 3

    env.client.get("/v1/updates?as=reader&order=asc&expand=true&wait=0.05")  # a full parked page
    assert env.client.get("/v1/digest?as=reader").json()["updates"]["unread"] == 3


def test_a_public_after_bound_does_not_move_another_readers_count(env):
    """Reads are tokenless. `after=` is this request's query bound and nothing
    more: a third party reading `digest?as=victim&after=<far future>` must not
    pin anything the victim's next digest or recovery would trust."""
    seed_agent(env.hub, "victim")
    seed_agent(env.hub, "writer")
    env.client.post("/v1/messages", json={"agent_id": "writer", "body": "@victim hello"})
    pinned = env.client.get("/v1/digest?as=victim&after=99991231-235959-999_attacker.md").json()
    assert pinned["updates"]["unread"] == 0  # this request's own bound, honestly answered
    assert env.client.get("/v1/digest?as=victim").json()["updates"]["unread"] == 1
    assert "last_cursor" not in (env.client.get("/v1/digest?as=victim").json()["watching"] or {})


def test_digest_updates_is_zero_for_a_quiet_handle(env):
    seed_agent(env.hub, "watcher")
    data = env.client.get("/v1/digest?as=watcher").json()
    assert data["updates"] == {"unread": 0, "newest": None}


def test_digest_watching_is_null_until_someone_watches(env):
    """Null = nobody is watching this handle. This is the signal that matters: a
    dead watcher is otherwise indistinguishable from a quiet inbox."""
    seed_agent(env.hub, "watcher")
    assert env.client.get("/v1/digest?as=watcher").json()["watching"] is None


def test_a_digest_read_counts_as_a_poll_but_does_not_answer_itself(env):
    """The digest reports presence as it stood BEFORE the read, then stamps it:
    the next digest sees the previous one."""
    seed_agent(env.hub, "watcher")
    assert env.client.get("/v1/digest?as=watcher").json()["watching"] is None
    block = env.client.get("/v1/digest?as=watcher").json()["watching"]
    assert block["mode"] == "poll" and block["stream"] == "digest"


def test_digest_watching_records_parked_and_plain_reads(env):
    """Any /v1/updates read stamps presence — a synchronous `--max-wait` caller
    is present between calls — and `mode` says whether it parked."""
    seed_agent(env.hub, "watcher")

    env.client.get("/v1/updates?as=watcher")  # wait=0
    block = env.client.get("/v1/digest?as=watcher").json()["watching"]
    assert (block["mode"], block["stream"]) == ("poll", "updates")

    env.client.get("/v1/updates?as=watcher&wait=0.15")
    block = env.client.get("/v1/digest?as=watcher").json()["watching"]
    assert (block["mode"], block["stream"]) == ("parked", "updates")
    assert block["last_poll_age_s"] >= 0


def test_a_digest_read_does_not_hide_a_parked_watcher(env):
    """mode follows the last PARKED poll, not the last read: a digest between
    two parks still reports parked, with the parked poll's stream."""
    seed_agent(env.hub, "watcher")
    env.client.get("/v1/updates?as=watcher&wait=0.05")
    env.client.get("/v1/digest?as=watcher")

    block = env.client.get("/v1/digest?as=watcher").json()["watching"]
    assert (block["mode"], block["stream"]) == ("parked", "updates")


def test_digest_watching_reports_the_stream(env):
    """inbox / feed / updates are distinguishable, so an organizer can see WHAT
    an agent is watching, not just that it is alive."""
    seed_agent(env.hub, "watcher")
    env.client.get("/v1/inbox/watcher?wait=0.1")
    block = env.client.get("/v1/digest?as=watcher").json()["watching"]
    assert (block["mode"], block["stream"]) == ("parked", "inbox")

    _create_channel(env, "room")
    _subscribe(env, "room", "watcher")
    env.client.get("/v1/channels/feed?as=watcher&wait=0.1")
    assert env.client.get("/v1/digest?as=watcher").json()["watching"]["stream"] == "feed"


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
        assert (seen["mode"], seen["stream"]) == ("parked", "updates")
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
