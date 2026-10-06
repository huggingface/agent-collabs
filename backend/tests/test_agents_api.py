import httpx
import pytest
from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError

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


BUCKET = "test-org/test-agent-9"
HANDSHAKE = ".bucket-sync-handshake"


def test_matching_handshake_is_still_proved_by_a_caller_write(env):
    env.hub.seed(HANDSHAKE, "test-user", bucket=BUCKET)
    env.hub.bucket_owners[BUCKET] = "test-user"
    assert _register(env).status_code == 201
    assert env.hub.created_buckets == []
    assert env.hub.caller_writes == [(BUCKET, HANDSHAKE, "hf_caller")]


def test_handshake_planted_in_someone_elses_bucket_is_refused(env):
    # An attacker pre-creates the bucket a victim will register and writes the
    # victim's public username into it. Matching text is not proof: the
    # victim's token cannot write there, so registration must refuse.
    env.hub.seed(HANDSHAKE, "test-user", bucket=BUCKET)
    env.hub.bucket_owners[BUCKET] = "attacker"
    r = _register(env)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "BUCKET_NOT_YOURS"
    assert env.hub.caller_writes == []
    assert env.client.get("/v1/agents").json()["count"] == 0


def _victim_state(env):
    """Victim's registration of agent-9 and the handshake in its bucket."""
    seed_agent(env.hub, "agent-9", hf_user="victim")
    env.hub.seed(HANDSHAKE, "victim", bucket=BUCKET)
    env.hub.bucket_owners[BUCKET] = "victim"
    env.hub.caller_is_org_admin = True  # the caller could write anywhere
    return dict(env.hub.buckets[env.settings.central_bucket]), dict(env.hub.buckets[BUCKET])


def _assert_untouched(env, before):
    central, bucket = before
    assert env.hub.buckets[env.settings.central_bucket] == central
    assert env.hub.buckets[BUCKET] == bucket
    assert env.hub.caller_writes == [] and env.hub.created_buckets == []


def test_registration_read_outage_aborts_before_provisioning(env):
    before = _victim_state(env)
    env.hub.failing_reads.add("agents/agent-9.md")
    r = _register(env, force=True)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "HUB_UNAVAILABLE"
    _assert_untouched(env, before)


def test_handshake_read_outage_aborts_before_writing(env):
    before = _victim_state(env)
    env.hub.buckets[env.settings.central_bucket].pop("agents/agent-9.md")
    before = (dict(env.hub.buckets[env.settings.central_bucket]), before[1])
    env.hub.failing_reads.add(f"{BUCKET}/{HANDSHAKE}")
    r = _register(env)
    assert r.status_code == 503
    _assert_untouched(env, before)


def test_foreign_handshake_blocks_even_a_caller_who_could_write(env):
    # An unregistered bucket whose handshake names another user is theirs; an
    # org admin's token could overwrite it, but registration must not.
    before = _victim_state(env)
    env.hub.buckets[env.settings.central_bucket].pop("agents/agent-9.md")
    before = (dict(env.hub.buckets[env.settings.central_bucket]), before[1])
    r = _register(env)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "BUCKET_NOT_YOURS"
    assert "victim" in r.json()["error"]["message"]
    _assert_untouched(env, before)


SECRET = "hf_SECRETtokenDoNotLeak123"


def test_caller_token_never_reaches_responses_or_logs(env, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    headers = {"authorization": f"Bearer {SECRET}"}
    body = {"agent_id": "agent-9", "model": "m", "harness": "h", "tools": []}
    post = lambda: env.client.post("/v1/agents/register", json=body, headers=headers)
    texts = []

    env.hub.whoami_fails = True                               # 401
    texts.append(post()); env.hub.whoami_fails = False
    env.hub.whoami_orgs = set()                               # 403 NOT_ORG_MEMBER
    texts.append(post()); env.hub.whoami_orgs = {env.settings.org}
    env.hub.failing_reads.add("agents/agent-9.md")            # 503 lookup outage
    texts.append(post()); env.hub.failing_reads.clear()

    def forbid(bucket, token):
        raise PermissionError(f"403 for token {token}")
    monkeypatch.setattr(env.hub, "create_bucket_as", forbid)  # 403 BUCKET_CREATE_FORBIDDEN
    texts.append(post()); monkeypatch.undo()
    env.hub.seed(HANDSHAKE, "test-user", bucket=BUCKET)       # 403 BUCKET_NOT_YOURS
    env.hub.bucket_owners[BUCKET] = "attacker"
    texts.append(post())
    env.hub.bucket_owners[BUCKET] = "test-user"               # 201, audited
    texts.append(post())

    assert [r.status_code for r in texts] == [401, 403, 503, 403, 403, 201]
    for r in texts:
        assert SECRET not in r.text
    assert SECRET not in caplog.text
    audit = b"".join(env.hub.buckets[env.settings.audit_bucket].values())
    assert SECRET.encode() not in audit


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


def test_optional_reads_map_only_missing_entries_to_none(env, monkeypatch):
    client = hub_module.HubClient(env.settings)

    def missing(**k):
        raise EntryNotFoundError("no such file")

    def outage(**k):
        raise _hub_error(500)

    monkeypatch.setattr(hub_module, "download_bucket_files", missing)
    assert client.read_text_optional("hf://buckets/test-org/test-agent-9/.bucket-sync-handshake") is None
    assert client.read_central_bytes_optional("agents/agent-9.md") is None
    monkeypatch.setattr(hub_module, "download_bucket_files", outage)
    with pytest.raises(HfHubHTTPError):
        client.read_text_optional("hf://buckets/test-org/test-agent-9/.bucket-sync-handshake")
    with pytest.raises(HfHubHTTPError):
        client.read_central_bytes_optional("agents/agent-9.md")
