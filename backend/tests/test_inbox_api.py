from fakes import seed_agent, seed_message


def test_raw_post_fans_out_to_mentions_and_humans(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": "agent-1", "body": "hi @agent-2, @human-cmpatino and @nobody"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["mentions_delivered"] == ["agent-2", "human-cmpatino"]

    central = env.hub.buckets[env.settings.central_bucket]
    filename = data["filename"]
    board = central[f"message_board/{filename}"]
    assert central[f"inbox/agent-2/{filename}"] == board  # byte-identical
    assert central[f"inbox/human-cmpatino/{filename}"] == board
    assert f"inbox/nobody/{filename}" not in central
    # board + both copies landed in ONE batch write
    assert env.hub.batch_writes[-1] == [
        f"message_board/{filename}",
        f"inbox/agent-2/{filename}",
        f"inbox/human-cmpatino/{filename}",
    ]


def test_inbox_read_after_write_and_expand(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    r = env.client.post(
        "/v1/messages", json={"agent_id": "agent-1", "body": "ping @agent-2"}
    )
    filename = r.json()["filename"]
    data = env.client.get("/v1/inbox/agent-2?expand=true").json()
    assert data["count"] == 1 and data["matched"] == 1
    assert data["items"][0]["filename"] == filename
    assert data["items"][0]["frontmatter"]["agent"] == "agent-1"
    # author filter uses the copied message's author
    assert env.client.get("/v1/inbox/agent-2?agent=agent-1").json()["matched"] == 1
    assert env.client.get("/v1/inbox/agent-2?agent=agent-2").json()["matched"] == 0


def test_refs_authors_get_a_copy(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    seed_message(env.hub, "20260601-100000-000", "agent-2", "earlier work")
    r = env.client.post(
        "/v1/messages",
        json={
            "agent_id": "agent-1",
            "body": "building on this",
            "refs": "20260601-100000-000_agent-2.md",
        },
    )
    assert r.json()["mentions_delivered"] == ["agent-2"]
    assert env.client.get("/v1/inbox/agent-2").json()["count"] == 1


def test_self_mention_is_not_delivered(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.post(
        "/v1/messages", json={"agent_id": "agent-1", "body": "note to @agent-1"}
    )
    assert r.json()["mentions_delivered"] == []


def test_fanout_cap(make_env):
    env = make_env(MENTION_FANOUT_CAP=2)
    seed_agent(env.hub, "agent-1")
    for i in range(2, 6):
        seed_agent(env.hub, f"agent-{i}")
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": "agent-1", "body": "@agent-2 @agent-3 @agent-4 @agent-5"},
    )
    assert r.json()["mentions_delivered"] == ["agent-2", "agent-3"]


def test_bucket_source_variant_fans_out(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    env.hub.seed(
        "drafts/plan.md",
        "---\ntype: agent\n---\nlong-form plan, cc @agent-2\n",
        bucket="test-org/test-agent-1",
    )
    r = env.client.post(
        "/v1/messages",
        json={"source": "hf://buckets/test-org/test-agent-1/drafts/plan.md"},
    )
    assert r.status_code == 201
    assert r.json()["via"] == "bucket"
    assert r.json()["mentions_delivered"] == ["agent-2"]


def test_human_inbox_is_readable_without_registration(env):
    assert env.client.get("/v1/inbox/human-cmpatino").json()["count"] == 0


def test_unregistered_agent_inbox_404s(env):
    r = env.client.get("/v1/inbox/ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"


def test_inbox_since_high_water_mark_polling(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "old @agent-2"})
    first = env.client.get("/v1/inbox/agent-2?limit=1").json()
    high_water = first["items"][0]
    env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "new @agent-2"})
    fresh = env.client.get(
        f"/v1/inbox/agent-2?expand=true&after={high_water}"
    ).json()
    assert [m["body"].strip() for m in fresh["items"]] == ["new @agent-2"]
    # `matched` counts filter matches; the cursor only trims the page
    assert fresh["matched"] == 2
