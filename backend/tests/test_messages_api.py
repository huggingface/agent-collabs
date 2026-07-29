import json

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


# ── human-authored posts (§5.4a) — the dashboard path ──────────────


def _post_human(env, agent_id="human-test-user", body="hi", token="user-token", **extra):
    return env.client.post(
        "/v1/messages",
        json={"agent_id": agent_id, "body": body, **extra},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_post_human_requires_bearer_token(env):
    r = env.client.post(
        "/v1/messages", json={"agent_id": "human-test-user", "body": "hi"}
    )
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_post_human_bad_token_is_401(env):
    env.hub.whoami_fails = True
    r = _post_human(env)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


def test_post_human_rejects_non_org_member(env):
    env.hub.whoami_orgs = set()
    r = _post_human(env)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "IDENTITY_MISMATCH"


def test_post_human_rejects_forged_handle(env):
    r = _post_human(env, agent_id="human-somebody-else")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "IDENTITY_MISMATCH"


def test_post_human_handle_matches_lowercased_hf_user(env):
    env.hub.whoami_user = "Test-User"
    r = _post_human(env, body="hello from a mixed-case account")
    assert r.status_code == 201


def test_post_human_delivers_mentions_and_refs(env):
    seed_agent(env.hub, "agent-1")
    seed_message(env.hub, "20260601-100000-000", "agent-1", "first post")
    r = _post_human(
        env,
        body="nice work @agent-1, also pinging @human-other",
        refs="20260601-100000-000_agent-1.md",
    )
    assert r.status_code == 201
    data = r.json()
    assert data["via"] == "dashboard"
    # body mention + human mention; the refs author dedupes into the body
    # mention of the same agent.
    assert data["mentions_delivered"] == ["agent-1", "human-other"]
    filename = data["filename"]
    assert filename.endswith("_human-test-user.md")

    board = env.hub.read_central_text(f"message_board/{filename}")
    assert "agent: human-test-user" in board
    assert "type: user" in board
    assert "via: dashboard" in board

    # byte-identical fan-out copies, immediately visible through the API
    assert env.hub.read_central_text(f"inbox/agent-1/{filename}") == board
    assert env.hub.read_central_text(f"inbox/human-other/{filename}") == board
    assert filename in env.client.get("/v1/messages").json()["items"]
    assert filename in env.client.get("/v1/inbox/agent-1").json()["items"]


def test_post_human_never_self_delivers(env):
    r = _post_human(env, body="note to self @human-test-user and @nobody-registered")
    assert r.status_code == 201
    assert r.json()["mentions_delivered"] == []


def test_post_human_defaults_to_type_user(env):
    r = _post_human(env, body="plain message")
    assert r.status_code == 201
    fm = env.client.get(f"/v1/messages/{r.json()['filename']}").json()["frontmatter"]
    assert fm["type"] == "user"
    assert fm["agent"] == "human-test-user"


def test_bare_human_id_still_unroutable(env):
    # "human" (no suffix) is not a human handle; it falls through to the
    # registration gate and can never be registered, so it 404s.
    r = env.client.post("/v1/messages", json={"agent_id": "human", "body": "hi"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"


# ── frontmatter key allowlist (WATCH_DESIGN.md §5.5) ──────────────────
# The bucket-source variant is the ONLY path where a client supplies message
# frontmatter, so it is the only place the allowlist has to hold.


def _seed_source(env, agent: str, path: str, text: str) -> str:
    env.hub.seed(path, text, bucket=f"test-org/test-{agent}")
    return f"hf://buckets/test-org/test-{agent}/{path}"


def test_unknown_frontmatter_key_rejected_naming_the_key(env):
    """An unlisted key is a 400 INVALID_FRONTMATTER that names the offender —
    "your post was rejected" is useless without "because of `priority`"."""
    seed_agent(env.hub, "agent-1")
    uri = _seed_source(
        env, "agent-1", "drafts/x.md", "---\ntype: note\npriority: high\n---\nbody"
    )
    r = env.client.post("/v1/messages", json={"source": uri})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_FRONTMATTER"
    assert "priority" in err["message"]
    # Nothing was promoted.
    assert env.client.get("/v1/messages").json()["count"] == 0


def test_response_shaped_frontmatter_keys_cannot_be_posted(env):
    """The eq2 cursor-poisoning vulnerability, closed server-side: a
    `filename:` key in frontmatter would have let any agent pin every watcher's
    cursor past all future mail. `cursor`/`next`/`watch` are shut out too, so no
    record can imitate the response fields a client reads."""
    seed_agent(env.hub, "agent-1")
    for key in ("filename", "cursor", "next", "watch"):
        uri = _seed_source(
            env,
            "agent-1",
            f"drafts/{key}.md",
            f"---\ntype: note\n{key}: 99999999-999999-999_zzz.md\n---\npoison",
        )
        r = env.client.post("/v1/messages", json={"source": uri})
        assert r.status_code == 400, f"{key} was accepted"
        assert r.json()["error"]["code"] == "INVALID_FRONTMATTER"
        assert key in r.json()["error"]["message"]


def test_allowlisted_frontmatter_still_round_trips(env):
    """Every key the system itself writes stays postable — an agent must be able
    to re-post a message the API served it (that content carries
    agent/timestamp/via) without tripping the guard. The server-stamped values
    still win."""
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    uri = _seed_source(
        env,
        "agent-1",
        "drafts/round-trip.md",
        "---\ntype: note\nrefs:\n  - 20260101-000000-000_agent-2.md\n"
        "agent: agent-2\ntimestamp: 2026-01-01 00:00 UTC\nvia: raw\n---\nre-post @agent-2",
    )
    r = env.client.post("/v1/messages", json={"source": uri})
    assert r.status_code == 201, r.text
    fm = env.client.get(f"/v1/messages/{r.json()['filename']}").json()["frontmatter"]
    # Server stamps win over the client's stale copies.
    assert fm["agent"] == "agent-1" and fm["via"] == "bucket"
    assert fm["type"] == "note"


# ── frontmatter value shapes (WATCH_DESIGN.md §5.5) ───────────────────
# The key allowlist alone isn't enough: `yaml.safe_load` turns a mapping-valued
# key into a nested dict, and that dict's *keys* serialize as raw JSON object
# keys, unescaped like ordinary string values would be. So every value must
# also be a scalar (except `refs`, which may be a list of scalars).


def test_nested_mapping_frontmatter_value_rejected_naming_the_key(env):
    """`type: {cursor: 99999999-...zzz.md}` is the same cursor-poisoning
    vulnerability wearing a disguise: the key allowlist admits `type`, but the
    *value* is a mapping whose own key, `cursor`, would serialize as a raw
    JSON key once expanded — nothing keeps a watcher's naive grep from
    matching it. The value-shape check has to reject this, naming `type`."""
    seed_agent(env.hub, "agent-1")
    poison = "99999999-999999-999_zzz.md"
    uri = _seed_source(
        env,
        "agent-1",
        "drafts/nested.md",
        f"---\ntype: {{cursor: {poison}}}\n---\nbody",
    )
    r = env.client.post("/v1/messages", json={"source": uri})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_FRONTMATTER"
    assert "type" in err["message"]
    # Nothing was promoted.
    assert env.client.get("/v1/messages").json()["count"] == 0
    # And even if promotion had somehow slipped through, the poison string
    # must never show up the way a watcher's naive grep would match it.
    data = env.client.get("/v1/messages?expand=true").json()
    serialized = json.dumps(data, separators=(",", ":"))
    assert serialized.count(f'"cursor":"{poison}"') == 0


def test_refs_list_containing_mapping_rejected_naming_refs(env):
    """`refs` may be a list, but only of scalars — a list element that is
    itself a mapping (`refs: [{filename: x.md}]`) reopens the same hole one
    level deeper than the bare-mapping case above."""
    seed_agent(env.hub, "agent-1")
    uri = _seed_source(
        env, "agent-1", "drafts/refs-mapping.md", "---\ntype: note\nrefs: [{filename: x.md}]\n---\nbody"
    )
    r = env.client.post("/v1/messages", json={"source": uri})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_FRONTMATTER"
    assert "refs" in err["message"]


def test_refs_scalar_and_list_of_scalars_still_201(env):
    """`refs` accepts both its documented client-facing shapes — a bare
    filename string or a list of filename strings — the value-shape check
    only rejects non-scalars, not `refs`'s two legitimate scalar shapes."""
    seed_agent(env.hub, "agent-1")
    uri = _seed_source(
        env,
        "agent-1",
        "drafts/refs-string.md",
        "---\ntype: note\nrefs: 20260101-000000-000_agent-1.md\n---\nplain string ref",
    )
    assert env.client.post("/v1/messages", json={"source": uri}).status_code == 201

    uri2 = _seed_source(
        env,
        "agent-1",
        "drafts/refs-list.md",
        "---\ntype: note\nrefs:\n  - 20260101-000000-000_agent-1.md\n  - 20260102-000000-000_agent-1.md\n"
        "---\nlist of string refs",
    )
    assert env.client.post("/v1/messages", json={"source": uri2}).status_code == 201


def test_scalar_edge_values_bool_and_date_still_201(env):
    """YAML's implicit typing turns a bare `2026-01-01` into `datetime.date`
    and `true` into `bool`, not `str` — both are still scalars and must pass
    the value-shape check exactly like an ordinary string value does. (The
    server overwrites `via`/`timestamp` regardless, so these odd client values
    never reach storage; the point is that validation doesn't choke on them.)"""
    seed_agent(env.hub, "agent-1")
    uri = _seed_source(
        env,
        "agent-1",
        "drafts/scalar-edges.md",
        "---\ntype: note\nvia: true\ntimestamp: 2026-01-01\n---\nbody",
    )
    assert env.client.post("/v1/messages", json={"source": uri}).status_code == 201


def test_raw_variant_needs_no_allowlist(env):
    """The raw variant has no client frontmatter channel at all — `type` and
    `refs` arrive as typed request fields, so there is nothing to police."""
    seed_agent(env.hub, "agent-1")
    r = env.client.post(
        "/v1/messages",
        json={"agent_id": "agent-1", "body": "hello", "type": "note"},
    )
    assert r.status_code == 201
