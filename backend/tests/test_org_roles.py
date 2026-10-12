"""Organizer roles: what HubClient reads from the Hub's role sources, and how
OrgRoles combines and caches them. Gate behaviour over HTTP is in
test_broadcast_api.py and test_channels_api.py."""
from __future__ import annotations

import pytest

from app import hub as hub_mod
from app.config import Settings
from app.hub import HubClient, HubIdentity, HubUnreachable, RolesNotExposed
from app.org_roles import OrgRoles, RoleUnknown
from fakes import FakeHub

httpx = hub_mod.httpx  # the httpx the HubClient session uses
MEMBERS = "https://huggingface.co/api/organizations/test-org/members"


def _settings(**extra) -> Settings:
    return Settings(
        HF_TOKEN="space-token",
        ORG="test-org",
        COLLAB_SLUG="test",
        AUDIT_BUCKET="auditor/test-audit",
        **extra,
    )


def _response(status=200, body=None, url=MEMBERS, link=None) -> httpx.Response:
    headers = {"Link": f'<{link}>; rel="next"'} if link else {}
    return httpx.Response(
        status, json=body, headers=headers, request=httpx.Request("GET", url)
    )


class _Session:
    """get_session() stand-in: answers each GET from a script, recording calls."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict | None, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params, headers or {}))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def session(monkeypatch):
    def install(*answers) -> _Session:
        s = _Session(*answers)
        monkeypatch.setattr(hub_mod, "get_session", lambda: s)
        return s

    return install


def _member(user, role=None):
    m = {"user": user, "fullname": user, "avatarUrl": "", "isPro": False,
         "type": "user", "_id": "0" * 24}
    if role is not None:
        m["role"] = role
    return m


# ── the members listing (Space token) ──────────────────────────────────


def test_members_listing_follows_the_link_cursor_to_the_end(session):
    s = session(
        _response(body=[_member("Alice", "admin")], link=f"{MEMBERS}?cursor=abc&limit=500"),
        _response(body=[_member("bob", "contributor")]),
    )
    roles = HubClient(_settings()).org_member_roles("test-org")
    assert roles == {"alice": "admin", "bob": "contributor"}
    assert s.calls[0][1] == {"limit": 500}  # the Hub's minimum is 10; offset isn't a parameter
    assert s.calls[1][0] == f"{MEMBERS}?cursor=abc&limit=500"
    assert s.calls[1][1] is None  # the cursor URL carries its own query
    assert s.calls[0][2]["authorization"] == "Bearer space-token"


def test_a_listing_without_role_fields_raises_never_an_empty_map(session):
    # The shape a repo-scoped token gets from the real Hub (seen 2026-10-12).
    session(_response(body=[_member("cmparra"), _member("cmpatino")]))
    with pytest.raises(RolesNotExposed):
        HubClient(_settings()).org_member_roles("test-org")


def test_a_later_page_without_roles_fails_the_whole_listing(session):
    session(
        _response(body=[_member("alice", "admin")], link=f"{MEMBERS}?cursor=p2"),
        _response(body=[_member("bob")]),
    )
    with pytest.raises(RolesNotExposed):
        HubClient(_settings()).org_member_roles("test-org")


@pytest.mark.parametrize(
    "answer",
    [_response(status=500), _response(status=403), httpx.ConnectError("down")],
    ids=["5xx", "403", "transport"],
)
def test_a_failed_page_raises(session, answer):
    session(_response(body=[_member("alice", "admin")], link=f"{MEMBERS}?cursor=p2"), answer)
    with pytest.raises(Exception) as exc:
        HubClient(_settings()).org_member_roles("test-org")
    assert not isinstance(exc.value, RolesNotExposed)  # an outage, not "no roles"


def test_a_listing_that_never_ends_raises(session, monkeypatch):
    monkeypatch.setattr(hub_mod, "_MEMBER_PAGES_MAX", 3)
    session(*[_response(body=[_member(f"u{i}", "read")], link=f"{MEMBERS}?cursor={i}")
              for i in range(3)])
    with pytest.raises(RuntimeError, match="did not end"):
        HubClient(_settings()).org_member_roles("test-org")


# ── /oauth/userinfo (caller's token) ───────────────────────────────────

USERINFO = "https://huggingface.co/oauth/userinfo"


def _userinfo(username="alice", orgs=()):
    return _response(body={"sub": "1", "preferred_username": username, "orgs": list(orgs)},
                     url=USERINFO)


def _org(handle, role=None, display=None):
    o = {"sub": "2", "name": display or handle, "preferred_username": handle, "picture": ""}
    if role is not None:
        o["roleInOrg"] = role
    return o


def test_userinfo_reads_the_role_for_the_org_handle(session):
    s = session(_userinfo(orgs=[_org("other", "admin"), _org("test-org", "admin")]))
    assert HubClient(_settings()).oauth_org_role("caller-token", "Alice", "test-org") == "admin"
    assert s.calls[0][0] == USERINFO
    assert s.calls[0][2]["authorization"] == "Bearer caller-token"


@pytest.mark.parametrize(
    "answer",
    [
        _response(status=401, url=USERINFO),  # a user access token, not OAuth
        _userinfo(username="mallory", orgs=[_org("test-org", "admin")]),
        _userinfo(orgs=[_org("test-org")]),  # org granted, role not shown
        _userinfo(orgs=[]),
        # a display name equal to our org's handle is not our org
        _userinfo(orgs=[_org("evil-org", "admin", display="test-org")]),
    ],
    ids=["not-oauth", "other-user", "no-role", "org-not-granted", "display-name"],
)
def test_userinfo_without_evidence_is_none(session, answer):
    session(answer)
    assert HubClient(_settings()).oauth_org_role("t", "alice", "test-org") is None


@pytest.mark.parametrize(
    "answer",
    [_response(status=503, url=USERINFO), _response(status=429, url=USERINFO),
     httpx.ReadTimeout("slow")],
    ids=["503", "429", "timeout"],
)
def test_userinfo_outage_raises_hub_unreachable(session, answer):
    session(answer)
    with pytest.raises(HubUnreachable):
        HubClient(_settings()).oauth_org_role("t", "alice", "test-org")


# ── whoami (caller's token) ────────────────────────────────────────────


def test_whoami_identity_keeps_each_orgs_role_when_exposed(monkeypatch):
    monkeypatch.setattr(hub_mod, "whoami", lambda token: {
        "name": "alice",
        "orgs": [
            {"name": "test-org", "roleInOrg": "admin"},
            {"name": "unscoped-org"},  # listed, role withheld
        ],
    })
    identity = HubClient(_settings()).whoami_identity("t")
    assert identity.orgs == {"test-org", "unscoped-org"}
    assert identity.roles == {"test-org": "admin"}


# ── OrgRoles: source order and cache ───────────────────────────────────


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _roles(hub, clock=None, ttl=300.0):
    return OrgRoles(hub, _settings(ORG_ROLES_TTL_S=ttl), clock=clock or _Clock())


def _me(roles=None):
    return HubIdentity("alice", {"test-org"}, roles or {})


@pytest.fixture
def hub():
    return FakeHub(_settings())


def test_cold_cache_fetches_once_then_serves_within_ttl(hub):
    hub.org_roles = {"alice": "admin"}
    clock = _Clock()
    roles = _roles(hub, clock)
    assert roles.role_of(_me(), "t") == "admin"
    clock.now = 299.0
    assert roles.role_of(_me(), "t") == "admin"
    assert hub.org_member_roles_calls == 1


def test_expiry_refetches_and_sees_a_demotion(hub):
    hub.org_roles = {"alice": "admin"}
    clock = _Clock()
    roles = _roles(hub, clock)
    assert roles.role_of(_me(), "t") == "admin"
    hub.org_roles = {"alice": "write"}
    clock.now = 300.0
    assert roles.role_of(_me(), "t") == "write"
    assert hub.org_member_roles_calls == 2


def test_a_refresh_failure_past_ttl_is_not_served_stale_or_cached(hub):
    hub.org_roles = {"alice": "admin"}
    clock = _Clock()
    roles = _roles(hub, clock)
    roles.role_of(_me(), "t")
    clock.now = 301.0
    hub.org_member_roles_fails = True
    with pytest.raises(RuntimeError):
        roles.role_of(_me(), "t")
    hub.org_member_roles_fails = False
    assert roles.role_of(_me(), "t") == "admin"
    assert hub.org_member_roles_calls == 3


def test_roles_not_exposed_is_role_unknown_and_not_cached(hub):
    hub.org_roles_exposed = False
    roles = _roles(hub)
    with pytest.raises(RoleUnknown):
        roles.role_of(_me(), "t")
    hub.org_roles_exposed = True
    hub.org_roles = {"alice": "admin"}
    assert roles.role_of(_me(), "t") == "admin"  # same instant: nothing was cached


def test_a_member_absent_from_a_complete_listing_has_no_role(hub):
    hub.org_roles = {"bob": "admin"}
    assert _roles(hub).role_of(_me(), "t") is None


def test_the_callers_own_role_wins_and_skips_every_lookup(hub):
    hub.oauth_role = "admin"
    hub.org_roles = {"alice": "admin"}
    assert _roles(hub).role_of(_me({"test-org": "read"}), "t") == "read"
    assert hub.oauth_role_calls == 0
    assert hub.org_member_roles_calls == 0


def test_a_userinfo_outage_is_raised_when_the_listing_has_no_roles(hub):
    hub.oauth_role_fails = True
    hub.org_roles_exposed = False
    with pytest.raises(HubUnreachable):
        _roles(hub).role_of(_me(), "t")
