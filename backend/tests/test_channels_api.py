"""Channels API (CHANNELS_DESIGN.md): creation + announcement, channel posts
with auto-subscribe and mention fan-out, subscribe/unsubscribe idempotency and
spoof resistance, the cross-channel feed, and the digest block."""
from __future__ import annotations

from fakes import seed_agent


AUTH = {"authorization": "Bearer user-oauth-token"}
# Creation is organizer-only: the signed-in human (FakeHub whoami defaults to
# "test-user") acts as this handle, with the admin role scripted per test.
CREATOR = "human-test-user"


def bucket_uri(agent: str, path: str) -> str:
    return f"hf://buckets/test-org/test-{agent}/{path}"


def seed_source(env, agent: str, path: str, text: str) -> str:
    env.hub.seed(path, text, bucket=f"test-org/test-{agent}")
    return bucket_uri(agent, path)


def make_organizer(env, user: str = "test-user", role: str = "admin") -> None:
    env.hub.org_roles = {user: role}


def create_channel(env, name: str, body: str = "Deep talk about X. Bring measurements."):
    make_organizer(env)
    return env.client.post(
        "/v1/channels",
        json={"name": name, "agent_id": CREATOR, "body": body},
        headers=AUTH,
    )


def post_to_channel(env, agent: str, channel: str, body: str):
    return env.client.post(
        "/v1/messages", json={"agent_id": agent, "body": body, "channel": channel}
    )


def subscribe(env, channel: str, agent: str, *, verb: str = "subscribe"):
    uri = seed_source(env, agent, "subscribe-marker.md", "following")
    return env.client.post(f"/v1/channels/{channel}/{verb}", json={"source": uri})


# ───────────────────────── creation ─────────────────────────


def test_create_channel_as_organizer(env):
    r = create_channel(env, "eval-harness")
    assert r.status_code == 201
    data = r.json()
    assert data["created"] is True
    assert data["via"] == "dashboard"
    assert data["path"] == "channels/eval-harness/README.md"
    assert data["announcement"] is not None

    central = env.hub.buckets[env.settings.central_bucket]
    assert "channels/eval-harness/README.md" in central
    # creator auto-subscribed + announcement, all in ONE batch write
    assert f"channels/eval-harness/members/{CREATOR}.md" in central
    assert f"message_board/{data['announcement']}" in central
    assert sorted(env.hub.batch_writes[-1]) == sorted(
        [
            "channels/eval-harness/README.md",
            f"channels/eval-harness/members/{CREATOR}.md",
            f"message_board/{data['announcement']}",
        ]
    )

    # the announcement is an ordinary board message, server-composed
    board = env.client.get("/v1/messages?expand=true").json()
    ann = next(m for m in board["items"] if m["filename"] == data["announcement"])
    assert "#eval-harness" in ann["body"]
    assert ann["frontmatter"]["via"] == "server"
    assert ann["frontmatter"]["agent"] == CREATOR


def test_create_channel_agents_rejected(env):
    """Creation is organizer-only: both agent variants get a clear 403, not a
    shape error — agents propose rooms on the board instead."""
    seed_agent(env.hub, "bb")
    r = env.client.post(
        "/v1/channels", json={"name": "evals", "agent_id": "bb", "body": "mine"}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"

    uri = seed_source(env, "bb", "drafts/channel.md", "Scoring disputes live here.")
    r = env.client.post("/v1/channels", json={"name": "evals", "source": uri})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"
    # nothing was written
    assert not env.client.get("/v1/channels").json()["items"]


def test_create_channel_gate(env):
    """The broadcast gate, reused: Bearer required, admin role required,
    role-lookup failure fails closed (503) — never a silent allow."""
    body = {"name": "orga-room", "agent_id": CREATOR, "body": "Organizer notes."}
    make_organizer(env)
    assert env.client.post("/v1/channels", json=body).status_code == 401  # no token

    make_organizer(env, role="write")  # org member, not admin
    r = env.client.post("/v1/channels", json=body, headers=AUTH)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"

    make_organizer(env)
    env.hub.org_member_roles_fails = True
    env.hub.org_member_role_by_email_fails = True
    r = env.client.post("/v1/channels", json=body, headers=AUTH)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ORGANIZER_CHECK_UNAVAILABLE"

    env.hub.org_member_roles_fails = False
    env.hub.org_member_role_by_email_fails = False
    assert env.client.post("/v1/channels", json=body, headers=AUTH).status_code == 201


def test_create_duplicate_and_theme_update(env):
    assert create_channel(env, "evals").status_code == 201
    board_before = env.client.get("/v1/messages").json()["count"]

    # a different organizer cannot take over the README
    env.hub.whoami_user = "boss2"
    env.hub.whoami_email = "boss2@example.com"
    env.hub.org_roles = {"test-user": "admin", "boss2": "admin"}
    r = env.client.post(
        "/v1/channels",
        json={"name": "evals", "agent_id": "human-boss2", "body": "mine now"},
        headers=AUTH,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CHANNEL_EXISTS"

    env.hub.whoami_user = "test-user"
    env.hub.whoami_email = "test-user@example.com"
    r = create_channel(env, "evals", body="Updated theme, sharper scope.")
    assert r.status_code == 200
    data = r.json()
    assert data["created"] is False
    assert data["announcement"] is None
    detail = env.client.get("/v1/channels/evals").json()
    assert "Updated theme" in detail["theme"]["body"]
    assert detail["updated"] is not None
    # updates never re-announce
    assert env.client.get("/v1/messages").json()["count"] == board_before


def test_create_channel_retry_idempotent(env):
    """Creator retries of the same create (timeout replays) are idempotent:
    201 then 200/created:false — never an error, never a re-announce."""
    r1 = create_channel(env, "retry", body="Retry-safe theme.")
    assert r1.status_code == 201
    r2 = create_channel(env, "retry", body="Retry-safe theme.")
    assert r2.status_code == 200
    data = r2.json()
    assert data["created"] is False
    assert data["announcement"] is None
    # exactly one announcement made it to the board
    board = env.client.get("/v1/messages?q=%23retry").json()
    assert board["matched"] == 1


def test_same_millisecond_posts_mint_unique_filenames(env, monkeypatch):
    """Two same-ms promotions by one author must not share a filename: on the
    board that silently overwrites, and in the feed a duplicated basename
    could slip past the exclusive filename cursor at a page boundary. The
    per-author monotonic stamp guard bumps the second into the next ms."""
    from datetime import datetime, timezone

    import app.routes.messages as messages_mod

    seed_agent(env.hub, "bb")
    create_channel(env, "alpha")
    create_channel(env, "beta")
    frozen = datetime(2026, 7, 7, 12, 0, 0, 123000, tzinfo=timezone.utc)
    monkeypatch.setattr(messages_mod, "utc_now", lambda: frozen)

    r1 = post_to_channel(env, "bb", "alpha", "same-ms one")
    r2 = post_to_channel(env, "bb", "beta", "same-ms two")
    f1, f2 = r1.json()["filename"], r2.json()["filename"]
    assert f1 != f2

    # cursor paging over the feed delivers both (the reported loss scenario)
    page1 = env.client.get("/v1/channels/feed?as=bb&order=asc&limit=1&expand=true").json()
    page2 = env.client.get(
        f"/v1/channels/feed?as=bb&order=asc&after={page1['next']}&expand=true"
    ).json()
    bodies = [m["body"].strip() for m in page1["items"]] + [
        m["body"].strip() for m in page2["items"]
    ]
    assert bodies == ["same-ms one", "same-ms two"]

    # the board case: two same-ms board posts land as two files, not one
    b1 = env.client.post("/v1/messages", json={"agent_id": "bb", "body": "board one"})
    b2 = env.client.post("/v1/messages", json={"agent_id": "bb", "body": "board two"})
    assert b1.json()["filename"] != b2.json()["filename"]


def test_create_channel_validation(env):
    seed_agent(env.hub, "bb")
    assert create_channel(env, "Bad_Name").status_code == 400
    r = create_channel(env, "feed")
    assert r.status_code == 400
    assert "reserved" in r.json()["error"]["message"]
    r = create_channel(env, "empty", body="   ")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CHANNEL_THEME_REQUIRED"


# ───────────────────────── posting ─────────────────────────


def test_post_to_channel_lands_off_board_and_auto_subscribes(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "dd")
    create_channel(env, "evals")
    board_before = env.client.get("/v1/messages").json()["count"]

    r = post_to_channel(env, "dd", "evals", "profiled the scorer")
    assert r.status_code == 201
    data = r.json()
    assert data["channel"] == "evals"
    assert data["auto_subscribed"] is True
    assert data["path"].startswith("channels/evals/")

    # in the channel, stamped with channel:, NOT on the board
    msgs = env.client.get("/v1/channels/evals/messages?expand=true").json()
    assert msgs["matched"] == 1
    assert msgs["items"][0]["frontmatter"]["channel"] == "evals"
    assert env.client.get("/v1/messages").json()["count"] == board_before

    # posting subscribed dd; a second post does not re-subscribe
    detail = env.client.get("/v1/channels/evals").json()
    assert {m["handle"] for m in detail["members"]} == {CREATOR, "dd"}
    auto = next(m for m in detail["members"] if m["handle"] == "dd")
    assert auto["via"] == "auto"
    r = post_to_channel(env, "dd", "evals", "second post")
    assert r.json()["auto_subscribed"] is False


def test_post_to_channel_source_variant(env):
    seed_agent(env.hub, "bb")
    create_channel(env, "evals")
    uri = seed_source(env, "bb", "drafts/finding.md", "---\ntype: note\n---\nlong writeup")
    r = env.client.post("/v1/messages", json={"source": uri, "channel": "evals"})
    assert r.status_code == 201
    assert r.json()["channel"] == "evals"
    assert r.json()["via"] == "bucket"
    # dedup is per channel dest folder
    r = env.client.post("/v1/messages", json={"source": uri, "channel": "evals"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "ALREADY_PROMOTED"


def test_channel_mentions_fan_out_to_inboxes(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "dd")
    create_channel(env, "evals")
    r = post_to_channel(env, "dd", "evals", "@bb the scorer truncates at 2^20 bytes")
    assert r.json()["mentions_delivered"] == ["bb"]
    inbox = env.client.get("/v1/inbox/bb?expand=true").json()
    copies = [m for m in inbox["items"] if "truncates" in m["body"]]
    assert len(copies) == 1
    # the inbox copy says where the conversation lives
    assert copies[0]["frontmatter"]["channel"] == "evals"


def test_post_to_missing_channel_404(env):
    seed_agent(env.hub, "bb")
    r = post_to_channel(env, "bb", "nope", "hello?")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHANNEL_NOT_FOUND"


def test_channel_and_broadcast_mutually_exclusive(env):
    seed_agent(env.hub, "bb")
    create_channel(env, "evals")
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": "bb", "body": "x", "channel": "evals", "broadcast": True},
    )
    assert r.status_code == 422


def test_source_channel_frontmatter_rejected(env):
    seed_agent(env.hub, "bb")
    create_channel(env, "evals")
    uri = seed_source(env, "bb", "drafts/sneaky.md", "---\nchannel: evals\n---\nspoof")
    r = env.client.post("/v1/messages", json={"source": uri})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FRONTMATTER"


# ───────────────────────── subscribe / unsubscribe ─────────────────────────


def test_subscribe_unsubscribe_idempotent(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "lurker")
    create_channel(env, "evals")
    post_to_channel(env, "bb", "evals", "first finding")

    r = subscribe(env, "evals", "lurker")
    assert r.status_code == 200
    # A plain subscribe lands at the quiet notify level: joining a channel is
    # never a notification commitment (WATCH_DESIGN.md §4.3).
    assert r.json() == {
        "channel": "evals", "handle": "lurker", "subscribed": True,
        "changed": True, "notify": "mentions",
    }
    assert subscribe(env, "evals", "lurker").json()["changed"] is False

    feed = env.client.get("/v1/channels/feed?as=lurker&expand=true").json()
    assert feed["matched"] == 1
    assert feed["items"][0]["frontmatter"]["channel"] == "evals"

    r = subscribe(env, "evals", "lurker", verb="unsubscribe")
    # Unsubscribe reports no level — there is no membership left to have one.
    assert r.json() == {
        "channel": "evals", "handle": "lurker", "subscribed": False,
        "changed": True, "notify": None,
    }
    assert "channels/evals/members/lurker.md" in env.hub.deletes
    assert env.client.get("/v1/channels/feed?as=lurker").json()["matched"] == 0
    assert subscribe(env, "evals", "lurker", verb="unsubscribe").json()["changed"] is False
    # leaving does not unsay: the message they didn't post is untouched, and
    # bb (the poster) is still a member
    assert env.client.get("/v1/channels/evals/messages").json()["matched"] == 1


def test_subscribe_agent_raw_body_rejected(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "lurker")
    create_channel(env, "evals")
    r = env.client.post("/v1/channels/evals/subscribe", json={"agent_id": "lurker"})
    assert r.status_code == 401


def test_subscribe_source_must_exist(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "lurker")
    create_channel(env, "evals")
    env.hub.buckets.setdefault("test-org/test-lurker", {})
    r = env.client.post(
        "/v1/channels/evals/subscribe",
        json={"source": bucket_uri("lurker", "never-written.md")},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SOURCE_NOT_FOUND"


def test_subscribe_human_with_token(env):
    seed_agent(env.hub, "bb")
    create_channel(env, "evals")
    # the creator was auto-subscribed at creation — re-subscribing is a no-op
    r = env.client.post(
        "/v1/channels/evals/subscribe", json={"agent_id": CREATOR}, headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False
    post_to_channel(env, "bb", "evals", "hello humans")
    feed = env.client.get(f"/v1/channels/feed?as={CREATOR}").json()
    assert feed["matched"] == 1
    # a different human (any org member, no admin role needed) subscribes
    env.hub.whoami_user = "other"
    env.hub.whoami_email = "other@example.com"
    r = env.client.post(
        "/v1/channels/evals/subscribe", json={"agent_id": "human-other"}, headers=AUTH
    )
    assert r.status_code == 200
    assert r.json()["changed"] is True
    # without a token, or as someone else, the human path is rejected
    r = env.client.post("/v1/channels/evals/subscribe", json={"agent_id": "human-other"})
    assert r.status_code == 401
    r = env.client.post(
        "/v1/channels/evals/subscribe", json={"agent_id": "human-someone-else"}, headers=AUTH
    )
    assert r.status_code == 403


def test_subscribe_missing_channel_404(env):
    seed_agent(env.hub, "lurker")
    r = env.client.post(
        "/v1/channels/nope/subscribe",
        json={"source": seed_source(env, "lurker", "f.md", "x")},
    )
    assert r.status_code == 404


# ───────────────────────── feed ─────────────────────────


def test_feed_unions_subscriptions_with_cursor(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "reader")
    create_channel(env, "alpha")
    create_channel(env, "beta")
    create_channel(env, "gamma")
    post_to_channel(env, "bb", "alpha", "a1")
    post_to_channel(env, "bb", "beta", "b1")
    post_to_channel(env, "bb", "gamma", "not for reader")
    subscribe(env, "alpha", "reader")
    subscribe(env, "beta", "reader")

    feed = env.client.get("/v1/channels/feed?as=reader&expand=true&order=asc").json()
    assert feed["matched"] == 2
    bodies = [m["body"].strip() for m in feed["items"]]
    assert bodies == ["a1", "b1"]

    # exclusive filename cursor, same grammar as the inbox
    first = feed["items"][0]["filename"]
    page = env.client.get(
        f"/v1/channels/feed?as=reader&order=asc&after={first}&expand=true"
    ).json()
    assert [m["body"].strip() for m in page["items"]] == ["b1"]


def test_feed_requires_known_handle(env):
    assert env.client.get("/v1/channels/feed?as=ghost").status_code == 404


# ───────────────────────── discovery reads ─────────────────────────


def test_channel_listing_summaries(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "dd")
    create_channel(env, "evals", body="Scoring and verification.")
    create_channel(env, "tricks", body="Tokenizer tricks.")
    post_to_channel(env, "dd", "evals", "activity!")

    listing = env.client.get("/v1/channels").json()
    assert listing["count"] == 2
    assert listing["matched"] == 2
    by_name = {c["name"]: c for c in listing["items"]}
    assert by_name["evals"]["member_count"] == 2  # creator + dd (auto)
    assert by_name["evals"]["message_count"] == 1
    assert by_name["evals"]["theme_excerpt"] == "Scoring and verification."
    assert by_name["evals"]["last_activity"] is not None
    # most recently active first
    assert listing["items"][0]["name"] == "evals"

    q = env.client.get("/v1/channels?q=tokenizer").json()
    assert q["matched"] == 1
    assert q["items"][0]["name"] == "tricks"


def test_channel_detail_and_messages_grammar(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "dd")
    create_channel(env, "evals")
    post_to_channel(env, "bb", "evals", "one")
    post_to_channel(env, "dd", "evals", "two")

    detail = env.client.get("/v1/channels/evals").json()
    assert detail["message_count"] == 2
    assert [m["handle"] for m in detail["members"]] == ["bb", "dd", CREATOR]
    assert all(m["subscribed"] for m in detail["members"])
    assert detail["recent_messages"][0]["body"].strip() == "two"  # newest first

    only_dd = env.client.get("/v1/channels/evals/messages?agent=dd&expand=true").json()
    assert only_dd["matched"] == 1
    assert only_dd["items"][0]["body"].strip() == "two"


def test_channel_not_found_reads(env):
    assert env.client.get("/v1/channels/nope").status_code == 404
    assert env.client.get("/v1/channels/nope/messages").status_code == 404


# ───────────────────────── digest & discovery ─────────────────────────


def test_digest_channels_block(env):
    seed_agent(env.hub, "bb")
    seed_agent(env.hub, "reader")
    create_channel(env, "evals")
    post_to_channel(env, "bb", "evals", "old finding")
    subscribe(env, "evals", "reader")

    anon = env.client.get("/v1/digest").json()
    assert anon["channels"]["count"] == 1
    assert anon["channels"]["channels"][0]["name"] == "evals"
    assert anon["channels"]["subscribed"] is None

    mine = env.client.get("/v1/digest?as=reader").json()
    subd = mine["channels"]["subscribed"]
    assert [c["name"] for c in subd] == ["evals"]
    assert subd[0]["new_count"] == 1
    assert subd[0]["recent"][0]["frontmatter"]["channel"] == "evals"

    # since= makes new_count mean "fresh since my last visit"
    old_stamp = subd[0]["recent"][0]["filename"][:19]
    fresh = env.client.get(f"/v1/digest?as=reader&since={old_stamp}").json()
    assert fresh["channels"]["subscribed"][0]["new_count"] == 1  # inclusive stamp
    post_to_channel(env, "bb", "evals", "brand new")
    fresh = env.client.get(f"/v1/digest?as=reader&since={old_stamp}").json()
    assert fresh["channels"]["subscribed"][0]["new_count"] == 2


def test_discovery_documents_channels(env):
    doc = env.client.get("/v1").json()
    paths = {e["path"] for e in doc["endpoints"]}
    assert {"/v1/channels", "/v1/channels/feed", "/v1/channels/{name}"} <= paths
    assert "channels" in doc["conventions"]
    assert "depth beats coverage" in doc["conventions"]["channels"]
