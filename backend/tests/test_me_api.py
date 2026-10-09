"""GET /v1/me — the caller's identity + organizer status, the dashboard's
hint for whether to show the broadcast toggle. Not the security boundary:
POST /v1/messages re-verifies on every broadcast."""

from app.frontmatter import serialise
from fakes import seed_agent


def _me(env, token="user-token"):
    return env.client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})


def test_me_requires_bearer_token(env):
    r = env.client.get("/v1/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_me_reports_organizer_via_email_lookup(env):
    env.hub.org_roles_by_email = {env.hub.whoami_email.lower(): ("test-user", "admin")}
    data = _me(env).json()
    assert data == {
        "hf_user": "test-user",
        "handle": "human-test-user",
        "is_member": True,
        "is_organizer": True,
        "traces": {"sessions": 0, "last_shared_at": None},
    }
    # the targeted lookup answered; no full-org scan needed
    assert env.hub.org_member_roles_calls == 0


def test_me_non_admin_member_is_not_organizer(env):
    env.hub.org_roles = {"test-user": "write"}
    data = _me(env).json()
    assert data["is_member"] is True
    assert data["is_organizer"] is False


def test_me_non_member_is_neither(env):
    env.hub.whoami_orgs = set()
    data = _me(env).json()
    assert data["is_member"] is False
    assert data["is_organizer"] is False
    # not a member → never bother resolving a role
    assert env.hub.org_member_role_by_email_calls == 0
    assert env.hub.org_member_roles_calls == 0


def test_me_degrades_to_not_organizer_on_lookup_failure(env):
    env.hub.org_member_role_by_email_fails = True
    env.hub.org_member_roles_fails = True
    r = _me(env)
    assert r.status_code == 200  # a UI hint must not 503 the whole page
    assert r.json()["is_organizer"] is False


def test_me_handle_is_lowercased(env):
    env.hub.whoami_user = "Test-User"
    assert _me(env).json()["handle"] == "human-test-user"


def test_me_sums_traces_of_the_callers_agents(env):
    seed_agent(env.hub, "mine-1")
    seed_agent(env.hub, "mine-2")
    seed_agent(env.hub, "theirs", hf_user="someone-else")
    for agent, at in [
        ("mine-1", "2026-06-01 10:00 UTC"),
        ("mine-2", "2026-06-02 10:00 UTC"),
        ("theirs", "2026-06-03 10:00 UTC"),
    ]:
        env.hub.seed(f"traces/{agent}/s/manifest.md", serialise({"promoted_at": at}, ""))
    assert _me(env).json()["traces"] == {
        "sessions": 2,
        "last_shared_at": "2026-06-02 10:00 UTC",
    }


def test_me_answers_with_an_empty_trace_summary_when_storage_fails(env):
    # The agents or traces folder can't be listed: identity and organizer
    # status still come back; only the trace summary is empty.
    env.hub.org_roles = {"test-user": "admin"}
    for folder in ("agents", "traces"):
        env.hub.fail_next_listing(folder)
        r = _me(env)
        assert r.status_code == 200, (folder, r.text)
        assert r.json()["is_organizer"] is True
        assert r.json()["traces"] == {"sessions": 0, "last_shared_at": None}
