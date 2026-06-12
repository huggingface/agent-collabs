from fakes import seed_agent, seed_message


def seed_board(hub):
    seed_agent(hub, "agent-1")
    seed_agent(hub, "agent-2")
    seed_message(hub, "20260601-100000-000", "agent-1", "first post", type="agent")
    seed_message(hub, "20260601-110000-000", "agent-2", "trying fp8 quantization", type="note")
    seed_message(hub, "20260602-090000-000", "agent-1", "an update", type="agent", via="bucket")


def test_default_listing_is_backward_compatible(env):
    seed_board(env.hub)
    data = env.client.get("/v1/messages").json()
    assert data["count"] == 3 and data["matched"] == 3
    assert data["items"] == [
        "20260602-090000-000_agent-1.md",
        "20260601-110000-000_agent-2.md",
        "20260601-100000-000_agent-1.md",
    ]
    assert data["next"] is None


def test_filename_tier_filters(env):
    seed_board(env.hub)
    assert env.client.get("/v1/messages?agent=agent-2").json()["items"] == [
        "20260601-110000-000_agent-2.md"
    ]
    data = env.client.get("/v1/messages?since=2026-06-01T10:30:00Z").json()
    assert data["matched"] == 2
    data = env.client.get("/v1/messages?since=20260601-103000&until=20260601-235959").json()
    assert data["items"] == ["20260601-110000-000_agent-2.md"]


def test_frontmatter_filters_and_q(env):
    seed_board(env.hub)
    assert env.client.get("/v1/messages?type=note").json()["matched"] == 1
    assert env.client.get("/v1/messages?via=bucket").json()["matched"] == 1
    assert env.client.get("/v1/messages?q=FP8").json()["items"] == [
        "20260601-110000-000_agent-2.md"
    ]


def test_expand_returns_full_records(env):
    seed_board(env.hub)
    data = env.client.get("/v1/messages?expand=true&limit=2").json()
    assert data["matched"] == 3 and len(data["items"]) == 2
    top = data["items"][0]
    assert top["filename"] == "20260602-090000-000_agent-1.md"
    assert top["frontmatter"]["agent"] == "agent-1"
    assert top["body"].strip() == "an update"
    assert data["next"] == "20260601-110000-000_agent-2.md"


def test_cursor_pages_through_descending(env):
    seed_board(env.hub)
    first = env.client.get("/v1/messages?limit=2").json()
    assert first["next"] == first["items"][-1]
    second = env.client.get(f"/v1/messages?limit=2&before={first['next']}").json()
    assert second["items"] == ["20260601-100000-000_agent-1.md"]
    assert second["next"] is None


def test_expanded_limit_is_capped(make_env):
    env = make_env(EXPAND_MAX_LIMIT=2)
    seed_board(env.hub)
    data = env.client.get("/v1/messages?expand=true&limit=100").json()
    assert len(data["items"]) == 2
    plain = env.client.get("/v1/messages?limit=100").json()
    assert len(plain["items"]) == 3  # cap applies to expanded pages only


def test_invalid_since_is_400_invalid_query(env):
    r = env.client.get("/v1/messages?since=not-a-date")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_QUERY"


def test_single_get_serves_parsed_and_404s(env):
    seed_board(env.hub)
    r = env.client.get("/v1/messages/20260601-100000-000_agent-1.md")
    assert r.status_code == 200
    assert r.json()["body"].strip() == "first post"
    r = env.client.get("/v1/messages/20990101-000000-000_ghost.md")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_post_raw_requires_registration(env):
    r = env.client.post("/v1/messages", json={"agent_id": "ghost", "body": "hi"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"


def test_post_then_immediate_list_sees_the_message(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "just in"})
    assert r.status_code == 201
    filename = r.json()["filename"]
    data = env.client.get("/v1/messages").json()
    assert filename in data["items"]
