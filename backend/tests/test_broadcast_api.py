"""Organizer broadcast: an admin-only message that lands on the board and
surfaces in every inbox via the read-time union (broadcasts/), not fan-out."""
from fakes import seed_agent


def _broadcast(env, body="heads up everyone", token="organizer-token", **extra):
    """Post a broadcast as the default test human (handle human-test-user)."""
    return env.client.post(
        "/v1/messages",
        json={"agent_id": "human-test-user", "body": body, "broadcast": True, **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


def _make_organizer(env, role="admin"):
    env.hub.org_roles = {"test-user": role}


def _make_organizer_by_email(env, role="admin", user="test-user"):
    assert env.hub.whoami_email is not None
    env.hub.org_roles_by_email = {env.hub.whoami_email.lower(): (user, role)}


# ── write path ────────────────────────────────────────────────────────


def test_organizer_broadcast_lands_on_board_and_in_broadcasts(env):
    _make_organizer(env)
    r = _broadcast(env)
    assert r.status_code == 201
    data = r.json()
    assert data["broadcast"] is True
    assert data["mentions_delivered"] == []  # union, not fan-out

    filename = data["filename"]
    central = env.hub.buckets[env.settings.central_bucket]
    board = central[f"message_board/{filename}"]
    # one shared copy, byte-identical, in the SAME batch write — no per-recipient copies
    assert central[f"broadcasts/{filename}"] == board
    assert env.hub.batch_writes[-1] == [
        f"message_board/{filename}",
        f"broadcasts/{filename}",
    ]
    # the flag is stamped for rendering/filtering, and round-trips
    fm = env.client.get(f"/v1/messages/{filename}").json()["frontmatter"]
    assert fm["broadcast"] is True
    assert fm["agent"] == "human-test-user"


def test_broadcast_surfaces_in_every_inbox_including_lurkers(env):
    seed_agent(env.hub, "agent-early")
    _make_organizer(env)
    filename = _broadcast(env, body="all hands").json()["filename"]

    # an existing agent sees it without being @-mentioned
    assert filename in env.client.get("/v1/inbox/agent-early").json()["items"]
    # a human who never posted and never registered still sees it via the union
    assert filename in env.client.get("/v1/inbox/human-newcomer").json()["items"]


def test_author_sees_their_own_broadcast(env):
    _make_organizer(env)
    filename = _broadcast(env).json()["filename"]
    assert filename in env.client.get("/v1/inbox/human-test-user").json()["items"]


def test_broadcast_appears_in_digest_inbox(env):
    seed_agent(env.hub, "agent-1")
    _make_organizer(env)
    filename = _broadcast(env, body="org-wide notice").json()["filename"]
    digest = env.client.get("/v1/digest?as=agent-1").json()
    assert filename in [m["filename"] for m in digest["inbox"]["items"]]


def test_inbox_union_merges_mentions_and_broadcasts(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    _make_organizer(env)
    # a direct mention copy lands in agent-1's own inbox folder
    env.client.post("/v1/messages", json={"agent_id": "agent-2", "body": "ping @agent-1"})
    # the broadcast reaches the same inbox via the union
    bfile = _broadcast(env, body="all hands").json()["filename"]
    data = env.client.get("/v1/inbox/agent-1?expand=true").json()
    assert bfile in [m["filename"] for m in data["items"]]
    assert any(m["body"].strip() == "ping @agent-1" for m in data["items"])
    assert data["matched"] == 2


def test_inbox_union_dedups_by_filename(env):
    # Defensive: the same filename present as both a fan-out copy and a
    # broadcast must surface exactly once.
    seed_agent(env.hub, "agent-1")
    fn = "20260601-120000-000_human-test-user.md"
    body = "---\nagent: human-test-user\ntype: user\nbroadcast: true\n---\nhello\n"
    env.hub.seed(f"inbox/agent-1/{fn}", body)
    env.hub.seed(f"broadcasts/{fn}", body)
    items = env.client.get("/v1/inbox/agent-1").json()["items"]
    assert items.count(fn) == 1


# ── the organizer gate ────────────────────────────────────────────────


def test_organizer_broadcast_uses_email_filtered_member_lookup(env):
    _make_organizer_by_email(env)
    r = _broadcast(env)
    assert r.status_code == 201
    assert env.hub.org_member_role_by_email_calls == 1
    assert env.hub.org_member_roles_calls == 0


def test_email_filtered_non_admin_cannot_broadcast_without_full_scan(env):
    _make_organizer_by_email(env, role="write")
    env.hub.org_roles = {"test-user": "admin"}  # would allow if fallback ran
    r = _broadcast(env)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"
    assert env.hub.org_member_role_by_email_calls == 1
    assert env.hub.org_member_roles_calls == 0


def test_broadcast_falls_back_to_full_member_scan_without_email(env):
    env.hub.whoami_email = None
    _make_organizer(env)
    r = _broadcast(env)
    assert r.status_code == 201
    assert env.hub.org_member_role_by_email_calls == 0
    assert env.hub.org_member_roles_calls == 1


def test_broadcast_falls_back_when_email_lookup_errors(env):
    env.hub.org_member_role_by_email_fails = True
    _make_organizer(env)
    r = _broadcast(env)
    assert r.status_code == 201
    assert env.hub.org_member_role_by_email_calls == 1
    assert env.hub.org_member_roles_calls == 1


def test_non_admin_member_cannot_broadcast(env):
    _make_organizer(env, role="write")  # a participant, not an organizer
    r = _broadcast(env)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"
    # nothing landed
    central = env.hub.buckets[env.settings.central_bucket]
    assert not any(k.startswith("broadcasts/") for k in central)
    assert not any(k.startswith("message_board/") for k in central)


def test_unknown_member_cannot_broadcast(env):
    env.hub.org_roles = {}  # caller absent from the role map
    r = _broadcast(env)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"


def test_agent_cannot_broadcast_raw(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.post(
        "/v1/messages", json={"agent_id": "agent-1", "body": "hi", "broadcast": True}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"


def test_agent_cannot_broadcast_via_source(env):
    seed_agent(env.hub, "agent-1")
    env.hub.seed(
        "drafts/x.md", "---\ntype: agent\n---\nbody\n", bucket="test-org/test-agent-1"
    )
    r = env.client.post(
        "/v1/messages",
        json={
            "source": "hf://buckets/test-org/test-agent-1/drafts/x.md",
            "broadcast": True,
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"


def test_agent_cannot_spoof_broadcast_frontmatter_via_source(env):
    seed_agent(env.hub, "agent-1")
    env.hub.seed(
        "drafts/x.md",
        "---\ntype: agent\nbroadcast: true\n---\nbody\n",
        bucket="test-org/test-agent-1",
    )
    r = env.client.post(
        "/v1/messages",
        json={"source": "hf://buckets/test-org/test-agent-1/drafts/x.md"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "NOT_ORGANIZER"
    central = env.hub.buckets[env.settings.central_bucket]
    assert not any(k.startswith("message_board/") for k in central)


def test_broadcast_requires_bearer_token(env):
    # No token → can't even resolve identity; same gate as any human post.
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": "human-test-user", "body": "hi", "broadcast": True},
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_broadcast_fails_closed_when_role_lookup_unavailable(env):
    env.hub.org_member_roles_fails = True
    r = _broadcast(env)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ORGANIZER_CHECK_UNAVAILABLE"
    # never silently downgraded to a normal post
    central = env.hub.buckets[env.settings.central_bucket]
    assert not any(k.startswith("message_board/") for k in central)


def test_non_broadcast_human_post_never_consults_roles(env):
    # A normal human post must not require the org-roles lookup at all.
    env.hub.org_member_roles_fails = True
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": "human-test-user", "body": "just a normal note"},
        headers={"Authorization": "Bearer user-token"},
    )
    assert r.status_code == 201
    assert r.json()["broadcast"] is False
