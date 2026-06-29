"""Trace & stats sharing: the promote (stats vs full), lenient validation +
completeness, idempotent stats->full upgrade, listing/detail, and the project
token aggregate (including null/coverage handling). All over the FakeHub."""
from __future__ import annotations

from app.frontmatter import serialise
from fakes import seed_agent


CC_USAGE = {
    "input_tokens": 1000,
    "output_tokens": 200,
    "cache_read_tokens": 5000,
    "cache_creation_tokens": 300,
    "total_tokens": 6500,
}


def _manifest(
    *,
    session_id="sess-1",
    harness="claude-code",
    model="claude-opus-4-8",
    usage=CC_USAGE,
    tool_calls=18,
    body="Swept BPE vocab; 32k won.",
    **extra,
):
    fm = {
        "schema_version": 1,
        "harness": harness,
        "session_id": session_id,
        "model": model,
        "started_at": "2026-06-25T13:50:11Z",
        "ended_at": "2026-06-25T14:30:11Z",
    }
    if usage is not None:
        fm["usage"] = usage
    if tool_calls is not None:
        fm["activity"] = {"tool_calls": tool_calls, "tool_calls_by_name": {"Bash": tool_calls}}
    fm.update(extra)
    return serialise(fm, body)


def _write_bundle(
    env,
    *,
    agent="agent-1",
    session="sess-1",
    manifest=None,
    log=None,
    log_name="session.jsonl",
):
    """Stage a bundle in the agent's own scratch bucket; return its source URI dir."""
    seed_agent(env.hub, agent)
    bucket = env.settings.agent_bucket(agent)  # test-org/test-<agent>
    env.hub.write_text_to_bucket(
        bucket, f"traces/{session}/manifest.md", manifest if manifest is not None else _manifest(session_id=session)
    )
    if log is not None:
        env.hub.write_bytes_to_bucket(bucket, f"traces/{session}/{log_name}", log)
    return f"hf://buckets/{bucket}/traces/{session}"


def _central(env):
    return env.hub.buckets[env.settings.central_bucket]


# ───────────────────────── promote: stats vs full ─────────────────────────

def test_stats_promote_writes_manifest_only(env):
    # A log is staged, but the default share=stats must NOT copy it.
    src = _write_bundle(env, manifest=_manifest(), log=b'{"type":"user"}\n')
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 201
    body = r.json()
    assert body["share"] == "stats"
    assert body["files_copied"] == 0
    assert body["completeness"] == "full"  # claude-code + tokens + tool_calls
    assert body["path"] == "traces/agent-1/sess-1/"

    central = _central(env)
    assert "traces/agent-1/sess-1/manifest.md" in central
    assert "traces/agent-1/sess-1/session.jsonl" not in central
    # server stamped identity into the manifest
    assert "agent: agent-1" in central["traces/agent-1/sess-1/manifest.md"].decode()
    assert "via: bucket" in central["traces/agent-1/sess-1/manifest.md"].decode()


def test_full_promote_hash_copies_the_log(env):
    src = _write_bundle(
        env,
        manifest=_manifest(native_log_file="session.jsonl"),
        log=b'{"type":"user"}\n{"type":"assistant"}\n',
    )
    r = env.client.post("/v1/traces", json={"source": src, "share": "full"})
    assert r.status_code == 201
    body = r.json()
    assert body["share"] == "full"
    assert body["files_copied"] == 1
    assert body["bytes_copied"] > 0

    central = _central(env)
    assert "traces/agent-1/sess-1/session.jsonl" in central
    # the stamped manifest records the promotion metadata
    assert "promoted_at:" in central["traces/agent-1/sess-1/manifest.md"].decode()


def test_full_promote_requires_declared_log_file(env):
    src = _write_bundle(env, manifest=_manifest(), log=b"{}\n")
    r = env.client.post("/v1/traces", json={"source": src, "share": "full"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FRONTMATTER"


def test_full_promote_copies_only_declared_log_file(env):
    src = _write_bundle(
        env,
        manifest=_manifest(native_log_file="session.jsonl"),
        log=b'{"current":true}\n',
    )
    bucket = env.settings.agent_bucket("agent-1")
    env.hub.write_bytes_to_bucket(bucket, "traces/sess-1/stale.jsonl", b'{"stale":true}\n')

    r = env.client.post("/v1/traces", json={"source": src, "share": "full"})
    assert r.status_code == 201
    assert r.json()["files_copied"] == 1

    central = _central(env)
    assert "traces/agent-1/sess-1/session.jsonl" in central
    assert "traces/agent-1/sess-1/stale.jsonl" not in central


def test_default_share_is_stats(env):
    src = _write_bundle(env, manifest=_manifest())
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.json()["share"] == "stats"


# ───────────────────────── validation & completeness ─────────────────────────

def test_missing_required_fields_rejected(env):
    bad = serialise({"harness": "claude-code"}, "no schema_version or session_id")
    src = _write_bundle(env, manifest=bad)
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_FRONTMATTER"


def test_negative_token_rejected(env):
    bad = _manifest(usage={"total_tokens": -5})
    src = _write_bundle(env, manifest=bad)
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 400


def test_fractional_token_rejected(env):
    bad = _manifest(usage={"total_tokens": 12.7})
    src = _write_bundle(env, manifest=bad)
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 400


def test_bad_timestamp_rejected(env):
    bad = _manifest(started_at="not-a-date")
    src = _write_bundle(env, manifest=bad)
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 400


def test_missing_manifest_rejected(env):
    seed_agent(env.hub, "agent-1")
    bucket = env.settings.agent_bucket("agent-1")
    env.hub.write_bytes_to_bucket(bucket, "traces/sess-1/session.jsonl", b"{}\n")
    r = env.client.post(
        "/v1/traces", json={"source": f"hf://buckets/{bucket}/traces/sess-1"}
    )
    assert r.status_code == 400


def test_unknown_harness_is_partial_but_accepted(env):
    # Graceful degradation: an unknown harness with no stats still promotes.
    m = _manifest(harness="cursor", usage=None, tool_calls=None)
    src = _write_bundle(env, manifest=m)
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 201
    assert r.json()["completeness"] == "partial"


def test_known_harness_without_tool_calls_is_partial(env):
    m = _manifest(tool_calls=None)  # claude-code + tokens but no activity
    src = _write_bundle(env, manifest=m)
    assert env.client.post("/v1/traces", json={"source": src}).json()["completeness"] == "partial"


# ───────────────────────── identity & idempotency ─────────────────────────

def test_not_registered_rejected(env):
    bucket = env.settings.agent_bucket("ghost")
    env.hub.write_text_to_bucket(bucket, "traces/s/manifest.md", _manifest(session_id="s"))
    r = env.client.post("/v1/traces", json={"source": f"hf://buckets/{bucket}/traces/s"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_REGISTERED"


def test_source_outside_own_bucket_rejected(env):
    seed_agent(env.hub, "agent-1")
    # a bucket in another org is rejected by resolve_source before anything else
    r = env.client.post(
        "/v1/traces",
        json={"source": "hf://buckets/other-org/test-agent-1/traces/s"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PATH"


def test_source_must_be_traces_session_dir(env):
    seed_agent(env.hub, "agent-1")
    bucket = env.settings.agent_bucket("agent-1")
    env.hub.write_text_to_bucket(bucket, "scratch/sess-1/manifest.md", _manifest())
    r = env.client.post(
        "/v1/traces", json={"source": f"hf://buckets/{bucket}/scratch/sess-1"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PATH"


def test_manifest_session_must_match_source_dir(env):
    bad = _manifest(session_id="other-session")
    src = _write_bundle(env, session="sess-1", manifest=bad)
    r = env.client.post("/v1/traces", json={"source": src})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_PATH"


def test_stats_then_full_upgrade(env):
    # stats first (no log), then full (with log) for the SAME session overwrites.
    src1 = _write_bundle(env, manifest=_manifest())
    assert env.client.post("/v1/traces", json={"source": src1}).json()["files_copied"] == 0
    # add a log and a full manifest to the bundle, then re-promote as full
    bucket = env.settings.agent_bucket("agent-1")
    env.hub.write_text_to_bucket(
        bucket,
        "traces/sess-1/manifest.md",
        _manifest(native_log_file="session.jsonl"),
    )
    env.hub.write_bytes_to_bucket(
        bucket, "traces/sess-1/session.jsonl", b"{}\n"
    )
    src2 = f"hf://buckets/{env.settings.agent_bucket('agent-1')}/traces/sess-1"
    r = env.client.post("/v1/traces", json={"source": src2, "share": "full"})
    assert r.status_code == 201
    assert r.json()["files_copied"] == 1
    assert "traces/agent-1/sess-1/session.jsonl" in _central(env)
    detail = env.client.get("/v1/traces/agent-1/sess-1").json()
    assert detail["log_files"] == ["traces/agent-1/sess-1/session.jsonl"]


# ───────────────────────── listing & detail ─────────────────────────

def test_list_and_detail(env):
    env.client.post(
        "/v1/traces",
        json={
            "source": _write_bundle(
                env,
                manifest=_manifest(native_log_file="session.jsonl"),
                log=b"{}\n",
            ),
            "share": "full",
        },
    )

    lst = env.client.get("/v1/traces?expand=true").json()
    assert lst["count"] == 1 and lst["matched"] == 1
    item = lst["items"][0]
    assert item["agent"] == "agent-1" and item["session_id"] == "sess-1"
    assert item["total_tokens"] == 6500 and item["tool_calls"] == 18
    assert item["harness"] == "claude-code"
    assert "Swept BPE" in item["summary_excerpt"]
    assert item["primary_log_file"] == "traces/agent-1/sess-1/session.jsonl"

    detail = env.client.get("/v1/traces/agent-1/sess-1").json()
    assert detail["frontmatter"]["usage"]["total_tokens"] == 6500
    assert "Swept BPE" in detail["body"]
    assert detail["log_files"] == ["traces/agent-1/sess-1/session.jsonl"]


def test_list_filters_by_harness(env):
    env.client.post("/v1/traces", json={"source": _write_bundle(env, agent="agent-1", session="a", manifest=_manifest(session_id="a", harness="claude-code"))})
    env.client.post("/v1/traces", json={"source": _write_bundle(env, agent="agent-2", session="b", manifest=_manifest(session_id="b", harness="codex"))})
    cc = env.client.get("/v1/traces?harness=claude-code&expand=true").json()
    assert cc["count"] == 2 and cc["matched"] == 1
    assert cc["items"][0]["harness"] == "claude-code"


def test_detail_not_found(env):
    assert env.client.get("/v1/traces/agent-1/nope").status_code == 404


# ───────────────────────── aggregate ─────────────────────────

def test_stats_aggregate_sums_and_counts_coverage(env):
    # agent-1: full tokens; agent-2: a minimal harness with NO tokens (coverage gap).
    env.client.post("/v1/traces", json={"source": _write_bundle(env, agent="agent-1", session="a", manifest=_manifest(session_id="a"))})
    env.client.post("/v1/traces", json={"source": _write_bundle(env, agent="agent-2", session="b", manifest=_manifest(session_id="b", harness="cursor", usage=None, tool_calls=None))})

    s = env.client.get("/v1/stats").json()
    assert s["tokens"]["total"] == 6500
    assert s["tokens"]["cache_read"] == 5000
    assert s["sessions_counted"] == 1
    assert s["sessions_missing_tokens"] == 1
    assert s["agents_reporting"] == 2
    assert s["by_agent"]["agent-1"]["total"] == 6500
    assert s["by_model"]["claude-opus-4-8"]["total"] == 6500
    assert s["by_day"]["2026-06-25"]["total"] == 6500


def test_stats_empty(env):
    s = env.client.get("/v1/stats").json()
    assert s["tokens"]["total"] == 0
    assert s["sessions_counted"] == 0 and s["agents_reporting"] == 0


def test_cost_summed_when_present(env):
    env.client.post("/v1/traces", json={"source": _write_bundle(env, manifest=_manifest(cost_usd=4.0))})
    assert env.client.get("/v1/stats").json()["cost_usd"] == 4.0
