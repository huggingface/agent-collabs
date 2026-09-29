"""Agent-facing errors at the hub boundary: every failure is the standard
{"error": {code, message, hint?}} body, never a bare 500 or a FastAPI 422."""
from __future__ import annotations

import logging

import httpx
import pytest
from huggingface_hub.errors import EntryNotFoundError, HfHubHTTPError, RepositoryNotFoundError

import app.hub as hub_module
from app.auth import HANDSHAKE_FILE
from app.errors import BucketNotOwnedByCaller
from app.routes.jobs import _verify_caller_owns_agent
from fakes import seed_agent


SOURCE = "hf://buckets/test-org/test-agent-1/drafts/note.md"
RESULT = "hf://buckets/test-org/test-agent-1/results/run1.md"
SOURCES = [("/v1/messages", SOURCE), ("/v1/results", RESULT)]
# Hub and Xet error text can carry signed URLs; none of it may reach a log
# line or a response.
MARKER = "SECRETMARKER"
SIGNED_URL = f"https://cas-bridge.xethub.hf.co/xet?X-Amz-Signature={MARKER}"


def _hub_error(status: int, cls=HfHubHTTPError) -> HfHubHTTPError:
    resp = httpx.Response(status, request=httpx.Request("GET", SIGNED_URL))
    return cls(f"{status} Client Error for url: {SIGNED_URL}", response=resp)


def _xet_error() -> ConnectionError:
    return ConnectionError(f"Network error: connection reset ({SIGNED_URL})")


@pytest.fixture
def real_reads(env, monkeypatch):
    """Source reads through the actual HubClient adapter, with only the network
    call mocked; returns a setter for what that call raises."""
    client = hub_module.HubClient(env.settings)
    monkeypatch.setattr(env.hub, "read_bytes_optional", client.read_bytes_optional)

    def fail_with(exc: Exception) -> None:
        def download(**kwargs):
            raise exc

        monkeypatch.setattr(hub_module, "download_bucket_files", download)

    return fail_with


@pytest.fixture
def real_writes(env, monkeypatch):
    """Central writes through the actual HubClient adapter; returns a setter for
    what batch_bucket_files raises."""
    client = hub_module.HubClient(env.settings)
    for name in ("write_many_central", "write_bytes_central", "write_text_central"):
        monkeypatch.setattr(env.hub, name, getattr(client, name))

    def fail_with(exc: Exception) -> None:
        def batch(**kwargs):
            raise exc

        monkeypatch.setattr(hub_module, "batch_bucket_files", batch)

    return fail_with


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


def test_failed_source_read_is_503_not_missing(env):
    seed_agent(env.hub, "agent-1")
    env.hub.seed("results/run1.md", "---\nscore: 1\n---\nx", bucket="test-org/test-agent-1")
    env.hub.fail_next_read("results/run1.md")
    _error(env.client.post("/v1/results", json={"source": RESULT}), 503, "STORAGE_UNAVAILABLE")


@pytest.mark.parametrize("route, uri", SOURCES)
@pytest.mark.parametrize(
    "missing",
    [EntryNotFoundError("no such file"), _hub_error(404, RepositoryNotFoundError)],
    ids=["file", "bucket"],
)
def test_missing_source_is_404_through_the_real_adapter(env, real_reads, route, uri, missing):
    seed_agent(env.hub, "agent-1")
    real_reads(missing)
    err = _error(env.client.post(route, json={"source": uri}), 404, "SOURCE_NOT_FOUND")
    assert "hf buckets cp" in err["hint"]


@pytest.mark.parametrize("route, uri", SOURCES)
@pytest.mark.parametrize("status", [429, 500, 503])
def test_failed_source_read_is_a_retryable_503(env, real_reads, route, uri, status):
    seed_agent(env.hub, "agent-1")
    real_reads(_hub_error(status))
    r = env.client.post(route, json={"source": uri})
    err = _error(r, 503, "STORAGE_UNAVAILABLE")
    assert "partly applied" in err["message"] and "deployment" not in err["message"]
    assert r.headers["retry-after"] == "5"


@pytest.mark.parametrize("route, uri", SOURCES)
def test_refused_source_read_is_a_deployment_503(env, real_reads, route, uri):
    seed_agent(env.hub, "agent-1")
    real_reads(_hub_error(403))
    err = _error(env.client.post(route, json={"source": uri}), 503, "STORAGE_UNAVAILABLE")
    assert "HTTP 403" in err["message"] and "tell the organizer" in err["message"]


@pytest.mark.parametrize("route, uri", SOURCES)
def test_xet_source_read_failure_is_a_sanitized_503(env, real_reads, caplog, route, uri):
    seed_agent(env.hub, "agent-1")
    real_reads(_xet_error())
    with caplog.at_level(logging.WARNING):
        r = env.client.post(route, json={"source": uri})
    _error(r, 503, "STORAGE_UNAVAILABLE")
    assert "type=ConnectionError" in caplog.text
    assert MARKER not in caplog.text and MARKER not in r.text


def test_storage_failure_logs_type_and_status_only(env, real_reads, caplog):
    seed_agent(env.hub, "agent-1")
    real_reads(_hub_error(503))
    with caplog.at_level(logging.WARNING):
        r = env.client.post("/v1/messages", json={"source": SOURCE})
    _error(r, 503, "STORAGE_UNAVAILABLE")
    assert "POST /v1/messages (type=HfHubHTTPError status=503)" in caplog.text
    assert MARKER not in caplog.text and MARKER not in r.text


@pytest.mark.parametrize(
    "exc",
    [_xet_error(), RuntimeError(f"Previous task error: 403 Forbidden {SIGNED_URL}")],
    ids=["xet-connection", "poisoned-session"],
)
def test_xet_write_failure_is_a_sanitized_503(env, real_writes, caplog, exc):
    seed_agent(env.hub, "agent-1")
    real_writes(exc)
    with caplog.at_level(logging.WARNING):
        r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "hi"})
    _error(r, 503, "STORAGE_UNAVAILABLE")
    assert r.headers["retry-after"] == "5"
    assert MARKER not in caplog.text and MARKER not in r.text


@pytest.mark.parametrize("status, reason", [(401, "Unauthorized"), (403, "Forbidden")])
@pytest.mark.parametrize("wrap", ["connection", "poisoned-session"])
def test_xet_refusal_of_the_space_token_is_the_deployment_503(env, real_writes, caplog, status, reason, wrap):
    refusal = (
        f"Network error: Request error: HTTP status client error ({status} {reason}), "
        f"domain: {SIGNED_URL}"
    )
    real_writes(ConnectionError(refusal) if wrap == "connection" else RuntimeError(f"Previous task error: {refusal}"))
    seed_agent(env.hub, "agent-1")
    with caplog.at_level(logging.WARNING):
        r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "hi"})
    err = _error(r, 503, "STORAGE_UNAVAILABLE")
    assert f"HTTP {status}" in err["message"] and "tell the organizer" in err["message"]
    assert f"status={status})" in caplog.text
    assert MARKER not in caplog.text and MARKER not in r.text


@pytest.mark.parametrize(
    "text",
    [
        "Network error: connection reset, domain: https://fake-hub.test/buckets/org/403/x?code=401",
        "Previous task error: request to https://fake-hub.test/403 timed out",
    ],
    ids=["connection", "poisoned-session"],
)
def test_a_401_or_403_only_in_a_url_is_not_a_refusal(env, real_writes, caplog, text):
    real_writes(ConnectionError(text) if text.startswith("Network") else RuntimeError(text))
    seed_agent(env.hub, "agent-1")
    with caplog.at_level(logging.WARNING):
        r = env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "hi"})
    err = _error(r, 503, "STORAGE_UNAVAILABLE")
    assert "deployment" not in err["message"]
    assert "status=None)" in caplog.text


def test_an_unrelated_runtime_error_is_not_a_storage_failure(env, real_writes):
    seed_agent(env.hub, "agent-1")
    real_writes(RuntimeError("a bug, not storage"))
    with pytest.raises(RuntimeError, match="a bug"):
        env.client.post("/v1/messages", json={"agent_id": "agent-1", "body": "hi"})


def test_download_many_raises_on_a_xet_failure_without_logging_its_text(env, monkeypatch, caplog):
    client = hub_module.HubClient(env.settings)

    def download(**kwargs):
        raise _xet_error()

    monkeypatch.setattr(hub_module, "download_bucket_files", download)
    with caplog.at_level(logging.WARNING), pytest.raises(hub_module.DownloadFailed) as caught:
        client.download_many(env.settings.central_bucket, ["agents/a.md"])
    assert "type=ConnectionError" in caplog.text and MARKER not in caplog.text
    assert MARKER not in str(caught.value)


def test_listing_failure_carries_no_exception_text(env, monkeypatch, caplog):
    """ListingFailed's message reaches logs and /v1/healthz's last_error."""
    client = hub_module.HubClient(env.settings)

    def tree(**kwargs):
        raise _hub_error(503)

    monkeypatch.setattr(hub_module, "list_bucket_tree", tree)
    with caplog.at_level(logging.WARNING), pytest.raises(hub_module.ListingFailed) as caught:
        client.list_central_dir("message_board")
    assert str(caught.value) == "type=HfHubHTTPError status=503"
    assert MARKER not in caplog.text


def test_jobs_handshake_read_failure_is_not_a_missing_handshake(env):
    bucket = "test-org/test-agent-1"
    env.hub.seed(HANDSHAKE_FILE, "test-user", bucket=bucket)
    env.hub.fail_next_read(HANDSHAKE_FILE)
    with pytest.raises(HfHubHTTPError):
        _verify_caller_owns_agent(env.hub, env.settings, "Bearer hf_caller", "agent-1", "test-user")
    del env.hub.buckets[bucket][HANDSHAKE_FILE]
    with pytest.raises(BucketNotOwnedByCaller):
        _verify_caller_owns_agent(env.hub, env.settings, "Bearer hf_caller", "agent-1", "test-user")


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
    # A failed write may still have landed: never promise it did not.
    assert "nothing was written" not in err["message"]
    assert "partly applied" in err["message"]
    assert "GET /v1/messages" in err["hint"]
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


def test_cold_listing_failure_is_503_not_an_empty_folder(env):
    env.hub.seed("message_board/20260601-100000-000_agent-1.md", "---\nagent: agent-1\n---\nhi")
    env.hub.fail_next_listing("message_board")
    r = env.client.get("/v1/messages")
    _error(r, 503, "STORAGE_UNAVAILABLE")
    assert r.headers["retry-after"] == "5"
    assert env.client.get("/v1/messages").json()["count"] == 1


def test_failed_batch_download_is_503_not_404(env):
    fn = "20260601-100000-000_agent-1.md"
    env.hub.seed(f"message_board/{fn}", "---\nagent: agent-1\n---\nhi")
    env.hub.fail_next_read("message_board/")
    _error(env.client.get(f"/v1/messages/{fn}"), 503, "STORAGE_UNAVAILABLE")
    assert env.client.get(f"/v1/messages/{fn}").status_code == 200
