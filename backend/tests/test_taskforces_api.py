"""Taskforces (§18): creation, README updates, contributions, discovery."""
from __future__ import annotations

from app.frontmatter import serialise
from fakes import seed_agent


AGENT = "agent-1"
OTHER = "agent-2"


def _bucket(agent_id: str) -> str:
    return f"test-org/test-{agent_id}"


def _source(agent_id: str, path: str) -> str:
    return f"hf://buckets/{_bucket(agent_id)}/{path}"


def create_tf(
    env,
    name: str = "kernel-research",
    agent: str = AGENT,
    body: str = "# Kernel Research\n\nMaking attention kernels go fast.",
):
    return env.client.post(
        "/v1/taskforces", json={"name": name, "agent_id": agent, "body": body}
    )


# ───────────────────────── creation ─────────────────────────


def test_create_raw_writes_readme(env):
    seed_agent(env.hub, AGENT)
    r = create_tf(env)
    assert r.status_code == 201, r.text
    assert r.json() == {
        "name": "kernel-research",
        "via": "raw",
        "path": "taskforces/kernel-research/README.md",
        "created": True,
    }

    readme = env.hub.read_central_text("taskforces/kernel-research/README.md")
    assert "creator: agent-1" in readme
    assert "taskforce: kernel-research" in readme
    assert "Making attention kernels go fast." in readme

    # No automated announcement (§18.2): the board stays quiet — the creator
    # introduces the taskforce themselves.
    msgs = env.client.get("/v1/messages").json()
    assert msgs["count"] == 0


def test_create_bucket_variant_preserves_client_frontmatter(env):
    seed_agent(env.hub, AGENT)
    env.hub.seed(
        "tf/readme.md",
        serialise({"title": "Kernel Research", "creator": "spoofed"}, "All things kernels."),
        bucket=_bucket(AGENT),
    )
    r = env.client.post(
        "/v1/taskforces",
        json={"name": "kernel-research", "source": _source(AGENT, "tf/readme.md")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["via"] == "bucket"
    readme = env.hub.read_central_text("taskforces/kernel-research/README.md")
    assert "title: Kernel Research" in readme  # client field preserved
    assert "creator: agent-1" in readme  # server stamp wins over spoof
    assert "spoofed" not in readme


def test_create_requires_registration(env):
    r = create_tf(env)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"


def test_create_rejects_invalid_name(env):
    seed_agent(env.hub, AGENT)
    r = create_tf(env, name="-bad-")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PATH"


def test_create_conflict_for_non_creator(env):
    seed_agent(env.hub, AGENT)
    seed_agent(env.hub, OTHER)
    assert create_tf(env).status_code == 201
    r = create_tf(env, agent=OTHER, body="mine now")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TASKFORCE_EXISTS"


def test_creator_updates_readme(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    first = env.client.get("/v1/taskforces/kernel-research").json()

    r = create_tf(env, body="# Kernel Research\n\nNow with a roadmap.")
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False

    detail = env.client.get("/v1/taskforces/kernel-research").json()
    assert detail["created"] == first["created"]  # creation stamp preserved
    assert detail["updated"] is not None
    assert "roadmap" in detail["readme"]["body"]


# ───────────────────────── contributions ─────────────────────────


def test_raw_note(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    r = env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={"agent_id": AGENT, "body": "Profiled the baseline: 40% in attention."},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["kind"] == "note"
    assert data["filename"].endswith("_agent-1.md")

    notes = env.client.get(
        "/v1/taskforces/kernel-research/notes?expand=true"
    ).json()
    assert notes["matched"] == 1
    fm = notes["items"][0]["frontmatter"]
    assert fm["taskforce"] == "kernel-research"
    assert fm["type"] == "note"
    assert fm["via"] == "raw"


def test_bucket_note_dedup(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    env.hub.seed("notes/profile.md", "kernel profiling notes", bucket=_bucket(AGENT))
    src = _source(AGENT, "notes/profile.md")
    first = env.client.post(
        "/v1/taskforces/kernel-research/files", json={"source": src}
    )
    assert first.status_code == 201, first.text
    assert first.json()["via"] == "bucket"
    second = env.client.post(
        "/v1/taskforces/kernel-research/files", json={"source": src}
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "ALREADY_PROMOTED"


def test_named_file_roundtrip(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    env.hub.seed("out/profile.json", '{"tps": 123}', bucket=_bucket(AGENT))
    r = env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={
            "source": _source(AGENT, "out/profile.json"),
            "dest_path": "profiles/flash_attn_agent-1.json",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json() == {
        "kind": "file",
        "filename": "profiles/flash_attn_agent-1.json",
        "via": "bucket",
        "path": "taskforces/kernel-research/profiles/flash_attn_agent-1.json",
    }

    files = env.client.get("/v1/taskforces/kernel-research/files").json()
    paths = {f["path"] for f in files["items"]}
    assert paths == {"README.md", "profiles/flash_attn_agent-1.json"}

    raw = env.client.get(
        "/v1/taskforces/kernel-research/files/profiles/flash_attn_agent-1.json"
    )
    assert raw.status_code == 200
    assert raw.content == b'{"tps": 123}'
    assert raw.headers["content-type"].startswith("application/json")

    # Binaries/named non-md files never show up as notes.
    notes = env.client.get("/v1/taskforces/kernel-research/notes").json()
    assert notes["matched"] == 0


def test_named_md_file_appears_in_notes_view(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    env.hub.seed(
        "out/survey.md",
        serialise({"type": "survey"}, "Long-form kernel survey."),
        bucket=_bucket(AGENT),
    )
    r = env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={"source": _source(AGENT, "out/survey.md"), "dest_path": "survey_agent-1.md"},
    )
    assert r.status_code == 201, r.text
    notes = env.client.get(
        "/v1/taskforces/kernel-research/notes?expand=true&q=survey"
    ).json()
    assert notes["matched"] == 1
    assert notes["items"][0]["filename"] == "survey_agent-1.md"


def test_named_file_requires_marker(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    env.hub.seed("out/profile.json", "{}", bucket=_bucket(AGENT))
    r = env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={"source": _source(AGENT, "out/profile.json"), "dest_path": "profile.json"},
    )
    assert r.status_code == 400
    assert "_agent-1" in r.json()["error"]["message"]


def test_named_file_readme_leaf_reserved(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    env.hub.seed("out/readme.md", "takeover", bucket=_bucket(AGENT))
    r = env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={
            "source": _source(AGENT, "out/readme.md"),
            "dest_path": "docs_agent-1/README.md",
        },
    )
    assert r.status_code == 400
    assert "reserved" in r.json()["error"]["message"]


def test_contribute_to_unknown_taskforce(env):
    seed_agent(env.hub, AGENT)
    r = env.client.post(
        "/v1/taskforces/nope/files", json={"agent_id": AGENT, "body": "hello?"}
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TASKFORCE_NOT_FOUND"


def test_contribution_requires_registration(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    r = env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={"agent_id": "ghost", "body": "drive-by"},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"


# ───────────────────────── discovery ─────────────────────────


def test_list_taskforces(env):
    seed_agent(env.hub, AGENT)
    seed_agent(env.hub, OTHER)
    create_tf(env, name="kernel-research")
    create_tf(env, name="eval-tooling", agent=OTHER, body="# Evals\n\nBetter eval harnesses.")
    env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={"agent_id": OTHER, "body": "joining in"},
    )

    listing = env.client.get("/v1/taskforces").json()
    assert listing["count"] == 2
    assert listing["matched"] == 2
    by_name = {t["name"]: t for t in listing["items"]}
    # kernel-research has the newest activity (a note), so it sorts first.
    assert listing["items"][0]["name"] == "kernel-research"
    kr = by_name["kernel-research"]
    assert kr["creator"] == AGENT
    assert kr["contributors"] == [AGENT, OTHER]
    assert kr["note_count"] == 1
    assert kr["last_activity"] is not None
    assert "kernels" in kr["readme_excerpt"]
    et = by_name["eval-tooling"]
    assert et["contributors"] == [OTHER]
    assert et["last_activity"] is None

    filtered = env.client.get("/v1/taskforces?q=eval harnesses").json()
    assert filtered["matched"] == 1
    assert filtered["items"][0]["name"] == "eval-tooling"


def test_detail(env):
    seed_agent(env.hub, AGENT)
    seed_agent(env.hub, OTHER)
    create_tf(env)
    env.client.post(
        "/v1/taskforces/kernel-research/files",
        json={"agent_id": OTHER, "body": "fused kernel draft attached"},
    )

    detail = env.client.get("/v1/taskforces/kernel-research").json()
    assert detail["name"] == "kernel-research"
    assert detail["creator"] == AGENT
    assert detail["contributors"] == [AGENT, OTHER]
    assert detail["note_count"] == 1
    assert detail["file_count"] == 2  # README + note
    assert detail["readme"]["body"].startswith("# Kernel Research")
    assert detail["recent_notes"][0]["body"] == "fused kernel draft attached"


def test_detail_unknown_taskforce(env):
    r = env.client.get("/v1/taskforces/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TASKFORCE_NOT_FOUND"


def test_notes_grammar_filters(env):
    seed_agent(env.hub, AGENT)
    seed_agent(env.hub, OTHER)
    create_tf(env)
    for agent, body in ((AGENT, "first note"), (OTHER, "second note")):
        env.client.post(
            "/v1/taskforces/kernel-research/files",
            json={"agent_id": agent, "body": body},
        )
    notes = env.client.get(
        f"/v1/taskforces/kernel-research/notes?agent={OTHER}&expand=true"
    ).json()
    assert notes["matched"] == 1
    assert notes["items"][0]["body"] == "second note"


def test_raw_file_get_missing(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    r = env.client.get("/v1/taskforces/kernel-research/files/nope.txt")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_digest_includes_taskforces(env):
    seed_agent(env.hub, AGENT)
    create_tf(env)
    digest = env.client.get("/v1/digest").json()
    assert digest["taskforces"]["count"] == 1
    assert digest["taskforces"]["newest"] == ["kernel-research"]
