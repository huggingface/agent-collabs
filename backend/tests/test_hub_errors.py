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
