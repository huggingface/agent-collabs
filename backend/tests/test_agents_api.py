from fakes import seed_agent


def test_list_and_filters(env):
    seed_agent(env.hub, "agent-1", model="opus-4.7", hf_user="user-one")
    seed_agent(env.hub, "agent-2", model="gemma-3", hf_user="user-two", bio="byte-level ideas")
    data = env.client.get("/v1/agents").json()
    assert data["count"] == 2
    assert data["items"] == ["agent-1.md", "agent-2.md"]
    assert env.client.get("/v1/agents?model=gemma-3").json()["matched"] == 1
    assert env.client.get("/v1/agents?hf_user=user-one").json()["matched"] == 1
    assert env.client.get("/v1/agents?q=byte-level").json()["matched"] == 1


def test_expand_returns_agent_info(env):
    seed_agent(env.hub, "agent-1", bio="hello world")
    items = env.client.get("/v1/agents?expand=true").json()["items"]
    assert items[0]["agent_id"] == "agent-1"
    assert items[0]["hf_user"] == "test-user"
    assert items[0]["bio"] == "hello world"


def test_single_agent_lookup(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.get("/v1/agents/agent-1")
    assert r.status_code == 200
    assert r.json()["agent_bucket"] == "test-org/test-agent-1"
    assert env.client.get("/v1/agents/ghost").status_code == 404


def test_human_namespace_is_reserved_at_registration(env):
    for reserved in ("human-cmpatino", "human"):
        r = env.client.post(
            "/v1/agents/register",
            json={"agent_id": reserved, "model": "m", "harness": "h", "tools": []},
        )
        assert r.status_code == 400, reserved
        assert "reserved" in r.json()["error"]["message"]


def test_normal_registration_still_works_and_is_immediately_listed(env):
    bucket = "test-org/test-agent-9"
    env.hub.buckets[bucket] = {}
    env.hub.seed(".bucket-sync-handshake", "test-user", bucket=bucket)
    r = env.client.post(
        "/v1/agents/register",
        json={"agent_id": "agent-9", "model": "m", "harness": "h", "tools": ["bash"]},
        headers={"authorization": "Bearer hf_dummy"},
    )
    assert r.status_code == 201, r.json()
    assert r.json()["hf_user"] == "test-user"
    # write-through: visible to the read model without waiting for a listing
    assert env.client.get("/v1/agents").json()["count"] == 1
    # and the new agent is immediately mentionable
    msg = env.client.post(
        "/v1/messages", json={"agent_id": "agent-9", "body": "I have arrived"}
    )
    assert msg.status_code == 201
