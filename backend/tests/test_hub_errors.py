"""Agent-facing errors at the hub boundary: every failure is the standard
{"error": {code, message, hint?}} body, never a bare 500 or a FastAPI 422."""
from __future__ import annotations

from fakes import seed_agent


SOURCE = "hf://buckets/test-org/test-agent-1/drafts/note.md"
RESULT = "hf://buckets/test-org/test-agent-1/results/run1.md"


def _error(r, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    err = r.json()["error"]
    assert err["code"] == code and err["message"]
    return err


def test_missing_message_source_is_404_with_upload_hint(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.post("/v1/messages", json={"source": SOURCE})
    err = _error(r, 404, "SOURCE_NOT_FOUND")
    assert SOURCE in err["message"]
    assert "hf buckets cp" in err["hint"]


def test_failed_source_read_is_404(env):
    seed_agent(env.hub, "agent-1")
    env.hub.seed("results/run1.md", "---\nscore: 1\n---\nx", bucket="test-org/test-agent-1")
    env.hub.fail_next_read("results/run1.md")
    _error(env.client.post("/v1/results", json={"source": RESULT}), 404, "SOURCE_NOT_FOUND")


def test_binary_source_is_400(env):
    seed_agent(env.hub, "agent-1")
    env.hub.buckets["test-org/test-agent-1"] = {"drafts/note.md": b"\xff\xfe\x00binary"}
    for route, uri in [("/v1/messages", SOURCE), ("/v1/results", SOURCE)]:
        err = _error(env.client.post(route, json={"source": uri}), 400, "INVALID_FRONTMATTER")
        assert "UTF-8" in err["message"]


def test_oversized_source_is_413_naming_the_limit(make_env):
    env = make_env(MESSAGE_MAX_BYTES=100)
    seed_agent(env.hub, "agent-1")
    env.hub.seed("drafts/note.md", "x" * 101, bucket="test-org/test-agent-1")
    err = _error(env.client.post("/v1/messages", json={"source": SOURCE}), 413, "TOO_LARGE")
    assert "100" in err["message"]


def test_hub_write_failure_is_503_with_retry_after(env):
    seed_agent(env.hub, "agent-1")
    env.hub.fail_next_write()
    r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "hi"})
    err = _error(r, 503, "STORAGE_UNAVAILABLE")
    assert "nothing was written" in err["message"]
    assert r.headers["retry-after"] == "5"
    assert env.client.get("/v1/messages").json()["count"] == 0


def test_missing_required_fields_is_400_listing_each(env):
    r = env.client.post("/v1/agents/register", json={"agent_id": "agent-1"})
    err = _error(r, 400, "INVALID_REQUEST")
    lines = err["message"].splitlines()
    assert "body.model: Field required" in lines
    assert "body.harness: Field required" in lines


def test_unknown_field_is_400_naming_it(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.post(
        "/v1/messages", json={"agent_id": "agent-1", "body": "hi", "mentions": ["agent-2"]}
    )
    err = _error(r, 400, "INVALID_REQUEST")
    assert "body.mentions: Extra inputs are not permitted" in err["message"]


def test_oversized_raw_body_is_413(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "x" * 40_000})
    err = _error(r, 413, "TOO_LARGE")
    assert "32768" in err["message"]


def test_raw_refs_accepts_a_list(env):
    seed_agent(env.hub, "agent-1")
    seed_agent(env.hub, "agent-2")
    refs = ["20260601-100000-000_agent-2.md"]
    r = env.client.post(
        "/v1/messages", json={"agent_id": "agent-1", "body": "building on it", "refs": refs}
    )
    assert r.status_code == 201, r.text
    assert r.json()["mentions_delivered"] == ["agent-2"]
    fm = env.client.get(f"/v1/messages/{r.json()['filename']}").json()["frontmatter"]
    assert fm["refs"] == refs
