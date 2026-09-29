import httpx
import pytest
from huggingface_hub.errors import HfHubHTTPError

import app.hub as hub_module
from app.deps import get_registration_limiter
from app.main import app as fastapi_app
from app.rate_limit import TokenBucket
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


def _register(env, agent_id="agent-9", **extra):
    return env.client.post(
        "/v1/agents/register",
        json={"agent_id": agent_id, "model": "m", "harness": "h", "tools": [], **extra},
        headers={"authorization": "Bearer hf_caller"},
    )


def test_fresh_registration_provisions_bucket_with_caller_token(env):
    r = _register(env)
    assert r.status_code == 201, r.json()
    bucket = "test-org/test-agent-9"
    assert env.hub.created_buckets == [(bucket, "hf_caller")]
    assert env.hub.caller_writes == [(bucket, ".bucket-sync-handshake", "hf_caller")]
    assert env.hub.buckets[bucket][".bucket-sync-handshake"] == b"test-user"


def test_matching_handshake_needs_no_writes(env):
    env.hub.seed(".bucket-sync-handshake", "test-user", bucket="test-org/test-agent-9")
    assert _register(env).status_code == 201
    assert env.hub.created_buckets == [] and env.hub.caller_writes == []


def test_own_bucket_without_handshake_gets_one(env):
    bucket = "test-org/test-agent-9"
    env.hub.buckets[bucket] = {}
    env.hub.bucket_owners[bucket] = "test-user"
    assert _register(env).status_code == 201
    assert env.hub.caller_writes == [(bucket, ".bucket-sync-handshake", "hf_caller")]


def test_non_member_gets_invite_url(make_env):
    env = make_env(INVITE_URL="https://hf.co/invite/abc")
    env.hub.whoami_orgs = {"other-org"}
    r = _register(env)
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == "NOT_ORG_MEMBER"
    assert "https://hf.co/invite/abc" in err["message"]
    assert env.hub.created_buckets == []


def test_non_member_without_invite_url_asks_organizer(env):
    env.hub.whoami_orgs = set()
    r = _register(env)
    assert "ask the organizer for the invite link" in r.json()["error"]["message"]


def test_foreign_bucket_is_not_yours(env):
    bucket = "test-org/test-agent-9"
    env.hub.seed(".bucket-sync-handshake", "someone-else", bucket=bucket)
    env.hub.bucket_owners[bucket] = "someone-else"
    r = _register(env)
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == "BUCKET_NOT_YOURS"
    assert "pick another agent_id" in err["hint"]
    assert env.hub.buckets[bucket][".bucket-sync-handshake"] == b"someone-else"
    assert env.client.get("/v1/agents").json()["count"] == 0


def test_transient_whoami_error_is_503_not_401(env):
    env.hub.whoami_unreachable = True
    r = _register(env)
    assert r.status_code == 503
    assert "retry" in r.json()["error"]["message"]
    assert env.hub.created_buckets == []


def test_bad_token_is_401(env):
    env.hub.whoami_fails = True
    assert _register(env).status_code == 401


def test_duplicate_without_force_says_it_is_yours(env):
    assert _register(env).status_code == 201
    r = _register(env)
    assert r.status_code == 409
    assert "already registered to you" in r.json()["error"]["message"]
    assert _register(env, force=True).status_code == 201


def test_rate_limit_is_keyed_by_user_after_auth(env):
    limiter = TokenBucket(capacity=1, refill_per_minute=1)
    fastapi_app.dependency_overrides[get_registration_limiter] = lambda: limiter
    # A failed auth burns nothing.
    env.hub.whoami_fails = True
    assert _register(env).status_code == 401
    env.hub.whoami_fails = False
    assert _register(env, agent_id="agent-a").status_code == 201
    # Same user, another agent_id: still limited.
    assert _register(env, agent_id="agent-b").status_code == 429
    # A different user is not throttled by the first user's attempts.
    env.hub.whoami_user = "other-user"
    assert _register(env, agent_id="agent-b").status_code == 201


def _hub_error(status):
    resp = httpx.Response(status, request=httpx.Request("GET", "https://hf.co/api/x"))
    return HfHubHTTPError(f"{status}", response=resp)


def test_bucket_exists_maps_only_404_to_false(env, monkeypatch):
    client = hub_module.HubClient(env.settings)

    def raise_(status):
        def f(*a, **k):
            raise _hub_error(status)
        return f

    monkeypatch.setattr(hub_module, "bucket_info", raise_(404))
    assert client.bucket_exists("test-org/x") is False
    monkeypatch.setattr(hub_module, "bucket_info", raise_(503))
    with pytest.raises(hub_module.HubUnreachable):
        client.bucket_exists("test-org/x")


def test_transient_bucket_check_is_503(env, monkeypatch):
    def unreachable(bucket):
        raise hub_module.HubUnreachable("503")

    monkeypatch.setattr(env.hub, "bucket_exists", unreachable)
    r = _register(env)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "HUB_UNAVAILABLE"
