"""Dashboard posts across the dashboard/backend boundary: the real
`post_message` handler and `_post_message_via_api` adapter talking to the
real bucket-sync app (in-memory FakeHub) through an httpx transport that can
lose the request or the answer. No network, no credentials.

Covers: a 200 replay is success (F3); a lost answer never leads to a second
write and never claims "nothing was posted"; a same-key retry reconciles to
the original message (F2); and the backend's own verdicts pass through."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / "backend"), str(REPO / "backend" / "tests")]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backend_conftest = _load("backend_conftest_for_dashboard", REPO / "backend/tests/conftest.py")
from fakes import _http_error  # noqa: E402  (backend/tests, on sys.path above)


@pytest.fixture
def env():
    fixture = backend_conftest.make_env.__wrapped__()
    make = next(fixture)
    yield make()
    next(fixture, None)  # clears the backend's dependency overrides


@pytest.fixture
def dash(monkeypatch):
    d = _load("dashboard_app_post_outcomes", REPO / "dashboard/app.py")
    d.BACKEND_API_URL = "https://backend.test"
    d.LOCAL_BUCKET_DIR = None
    d.HF_TOKEN = "hf_space_token"
    d.direct_writes = []
    monkeypatch.setattr(d, "_write_message_hub", lambda *a: d.direct_writes.append(a))
    monkeypatch.setattr(d, "_write_message_local", lambda *a: d.direct_writes.append(a))
    return d


class Backend:
    """An httpx transport onto the backend app. `fault` decides each request's
    fate: None delivers it; "not-sent" fails to connect; "answer-lost"
    delivers it, then loses the response."""

    def __init__(self, env):
        self.env = env
        self.faults: list[str | None] = []
        self.statuses: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        fault = self.faults.pop(0) if self.faults else None
        if fault == "not-sent":
            raise httpx.ConnectError("connection refused", request=request)
        r = self.env.client.post(
            "/v1/messages",
            json=json.loads(request.content),
            headers={"Authorization": request.headers["authorization"]},
        )
        self.statuses.append(r.status_code)
        if fault == "answer-lost":
            raise httpx.ReadTimeout("answer lost", request=request)
        return httpx.Response(r.status_code, content=r.content, headers=r.headers)


@pytest.fixture
def backend(env, dash):
    b = Backend(env)
    dash.app.state.plain_client = httpx.AsyncClient(transport=httpx.MockTransport(b))
    return b


def _post(dash, **post):
    request = Request({"type": "http", "session": {"user": "test-user", "access_token": "user-token"}})
    try:
        return 200, asyncio.run(dash.post_message(dash.MessagePost(**post), request))
    except HTTPException as e:
        return e.status_code, e.detail


def _stored(env, folder="message_board/"):
    return sorted(k for k in env.hub.buckets[env.settings.central_bucket] if k.startswith(folder))


def _variant(env, variant):
    """The post fields for a board message, a channel post or a broadcast."""
    if variant == "channel":
        env.hub.org_roles = {"test-user": "admin"}
        r = env.client.post(
            "/v1/channels",
            json={"name": "room", "agent_id": "human-test-user", "body": "A room."},
            headers={"Authorization": "Bearer user-token"},
        )
        assert r.status_code == 201, r.text
        return {"channel": "room"}, "channels/room/"
    if variant == "broadcast":
        env.hub.org_roles = {"test-user": "admin"}
        return {"broadcast": True}, "broadcasts/"
    return {}, "message_board/"


VARIANTS = ["board", "channel", "broadcast"]


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_backend_200_replay_is_success_with_the_original_message(env, dash, backend, variant):
    """F3: the backend answers an identical retry with 200 and the first
    post's response; the dashboard returns it, and nothing is stored twice."""
    fields, folder = _variant(env, variant)
    before = len(_stored(env, folder))
    s1, first = _post(dash, body="same words", **fields)
    s2, second = _post(dash, body="same words", **fields)
    assert backend.statuses == [201, 200]
    assert (s1, s2) == (200, 200), (first, second)
    assert second["item"]["filename"] == first["item"]["filename"]
    assert len(_stored(env, folder)) == before + 1
    assert dash.direct_writes == []


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_lost_answer_is_unconfirmed_and_the_same_key_retry_reconciles(env, dash, backend, variant):
    """F2: the backend commits, the answer is lost. The dashboard neither
    writes again nor says nothing was posted; the retry with the same key
    gets the backend's replay of the original."""
    fields, folder = _variant(env, variant)
    before = len(_stored(env, folder))
    backend.faults = ["answer-lost"]
    status, detail = _post(dash, body="hello", idempotency_key="draft-1", **fields)
    assert status == 504
    assert "may have gone through" in detail and "nothing was posted" not in detail
    assert dash.direct_writes == []
    assert len(_stored(env, folder)) == before + 1  # the backend's one commit

    status, retried = _post(dash, body="hello", idempotency_key="draft-1", **fields)
    assert status == 200
    assert backend.statuses == [201, 200]
    assert len(_stored(env, folder)) == before + 1
    assert retried["item"]["filename"] in {k.rsplit("/", 1)[1] for k in _stored(env, folder)}


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_request_that_never_left_says_this_attempt_was_not_sent(env, dash, backend, variant):
    fields, folder = _variant(env, variant)
    before = len(_stored(env, folder))
    backend.faults = ["not-sent"]
    status, detail = _post(dash, body="hello", idempotency_key="draft-2", **fields)
    assert status == 503
    assert "this attempt wasn't sent" in detail
    assert dash.direct_writes == []  # no direct-write fallback
    assert len(_stored(env, folder)) == before
    assert _post(dash, body="hello", idempotency_key="draft-2", **fields)[0] == 200
    assert len(_stored(env, folder)) == before + 1


@pytest.mark.parametrize("variant", VARIANTS)
def test_a_retry_that_cannot_connect_does_not_settle_an_unconfirmed_post(env, dash, backend, variant):
    """The first attempt commits and its answer is lost; the retry (same key)
    can't connect. The retry's error is about that attempt only: it never
    says the message wasn't posted, because the first attempt's is stored."""
    fields, folder = _variant(env, variant)
    before = len(_stored(env, folder))
    backend.faults = ["answer-lost", "not-sent"]
    first = _post(dash, body="same message", idempotency_key="draft-9", **fields)
    retry = _post(dash, body="same message", idempotency_key="draft-9", **fields)
    assert first[0] == 504 and "may have gone through" in first[1]
    assert retry[0] == 503 and "this attempt wasn't sent" in retry[1]
    assert "not posted" not in retry[1]
    assert backend.statuses == [201]
    assert len(_stored(env, folder)) == before + 1
    assert dash.direct_writes == []


def test_a_partial_write_passes_the_backends_503_through(env, dash, backend, monkeypatch):
    """The backend's own 503 STORAGE_UNAVAILABLE ("may have been partly
    applied") is its verdict: shown as is, with no fallback write. The backend
    recorded nothing for replay, so a retry posts again: the documented limit
    of a 503."""
    write = env.hub.write_many_central

    def partial(items):
        write(items[:1])  # the message lands, the rest of the batch fails
        raise _http_error()

    monkeypatch.setattr(env.hub, "write_many_central", partial)
    status, detail = _post(dash, body="hello @agent-x", idempotency_key="draft-3")
    assert status == 503
    assert "partly applied" in detail
    assert dash.direct_writes == []
    assert len(_stored(env)) == 1


def test_overlapping_retries_post_once(env, dash, backend, monkeypatch):
    """A retry that arrives while the first request is still writing waits for
    it (the backend's per-key claim) and replays its response."""
    entered, release = threading.Event(), threading.Event()
    write = env.hub.write_many_central
    calls = []

    def held(items):
        calls.append(items)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
        return write(items)

    monkeypatch.setattr(env.hub, "write_many_central", held)
    results = {}

    def post(name):
        results[name] = _post(dash, body="overlap", idempotency_key="draft-4")

    first = threading.Thread(target=post, args=("first",))
    first.start()
    assert entered.wait(5)
    retry = threading.Thread(target=post, args=("retry",))
    retry.start()
    retry.join(0.3)
    assert retry.is_alive()  # waiting on the first request
    release.set()
    first.join(5)
    retry.join(5)
    assert results["first"][0] == results["retry"][0] == 200
    assert results["first"][1]["item"]["filename"] == results["retry"][1]["item"]["filename"]
    assert len(calls) == 1 and len(_stored(env)) == 1


def test_a_backend_restart_forgets_the_key(env, dash, backend):
    """The replay lives in the backend's memory: after a restart the same key
    posts again. Documented, not hidden."""
    backend.faults = ["answer-lost"]
    _post(dash, body="hello", idempotency_key="draft-5")
    _restart_backend_memory()
    assert _post(dash, body="hello", idempotency_key="draft-5")[0] == 200
    assert len(_stored(env)) == 2


def _restart_backend_memory():
    from app.deps import get_recent_posts
    from app.dedup import RecentPosts
    from app.main import app as fastapi_app

    fresh = RecentPosts(10_000)
    fastapi_app.dependency_overrides[get_recent_posts] = lambda: fresh


def test_a_rate_limit_stays_authoritative(env, dash, backend):
    from app.deps import get_raw_message_limiter
    from app.main import app as fastapi_app
    from app.rate_limit import CompoundLimiter, TokenBucket

    spent = CompoundLimiter(
        TokenBucket(capacity=1, refill_per_minute=0.001),
        TokenBucket(capacity=1, refill_per_minute=0.001),
    )
    spent.try_consume("human-test-user")
    fastapi_app.dependency_overrides[get_raw_message_limiter] = lambda: spent
    assert _post(dash, body="hello", idempotency_key="draft-6")[0] == 429
    assert dash.direct_writes == [] and _stored(env) == []


def test_a_refused_identity_is_a_session_expiry_not_a_fallback(env, dash, backend):
    env.hub.whoami_orgs = set()  # the backend's 403: not a member
    status, detail = _post(dash, body="hello", idempotency_key="draft-6")
    assert (status, detail) == (401, "Session expired. Please sign in again.")
    assert dash.direct_writes == [] and _stored(env) == []


def test_a_broadcast_by_a_non_organizer_is_refused_as_the_backend_says(env, dash, backend):
    env.hub.org_roles = {"test-user": "write"}
    status, detail = _post(dash, body="all hands", broadcast=True, idempotency_key="draft-7")
    assert status == 403
    assert "not an admin" in detail


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, text="<html>Bad Gateway</html>"),
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(201, json={"via": "dashboard"}),  # no filename
        httpx.Response(200, json={"filename": "../../etc/passwd"}),
        httpx.Response(201, text="not json"),
    ],
    ids=["proxy-502", "crash-500", "no-filename", "bad-filename", "not-json"],
)
def test_an_answer_that_doesnt_say_what_happened_is_unconfirmed(dash, response):
    dash.app.state.plain_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response)
    )
    status, detail = _post(dash, body="hello", idempotency_key="draft-8")
    assert status == 504 and "may have gone through" in detail
    assert dash.direct_writes == []


def test_the_key_is_forwarded_and_validated(dash):
    seen = []

    def answer(request):
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"filename": "20261012-090000-000_human-test-user.md"})

    dash.app.state.plain_client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    assert _post(dash, body="hi", idempotency_key="abc-123_X")[0] == 200
    assert seen[0]["idempotency_key"] == "abc-123_X"
    assert _post(dash, body="hi")[0] == 200
    assert "idempotency_key" not in seen[1]  # the backend's automatic identity applies
    with pytest.raises(Exception):
        dash.MessagePost(body="hi", idempotency_key="has spaces")


def test_an_unforeseen_adapter_error_is_unconfirmed_not_a_crash(dash, monkeypatch):
    async def boom(*a, **kw):
        raise TimeoutError("synthetic")

    monkeypatch.setattr(dash, "_post_message_via_api", boom)
    for fields in ({}, {"channel": "room"}, {"broadcast": True}):
        status, detail = _post(dash, body="hello", **fields)
        assert status == 504 and "may have gone through" in detail
    assert dash.direct_writes == []
