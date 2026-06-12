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
