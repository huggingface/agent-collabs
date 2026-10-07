"""/api/health in hub mode: healthy only when the token can list the bucket."""
import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def dash():
    spec = importlib.util.spec_from_file_location("dashboard_app_under_test", APP)
    d = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(d)
    d.LOCAL_BUCKET_DIR = None
    d.HF_TOKEN = "hf_dummy"
    d.BUCKET = "org/bucket"
    return d


class _Client:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    async def get(self, url, **kw):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return httpx.Response(self.outcome, request=httpx.Request("GET", url))


def _health_twice(d, outcome):
    """Status of two consecutive /api/health calls (the second is cached),
    the 503 detail if any, and how many Hub requests were made."""
    d._health_cache.update(ts=0.0, detail=None)
    client = _Client(outcome)
    d.app.state.client = client
    statuses, detail = [], None

    async def run():
        nonlocal detail
        for _ in range(2):
            try:
                await d.health()
                statuses.append(200)
            except d.HTTPException as e:
                statuses.append(e.status_code)
                detail = e.detail

    asyncio.run(run())
    return statuses, detail, client.calls


def test_listable_bucket_is_healthy(dash):
    assert _health_twice(dash, 200) == ([200, 200], None, 1)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_any_non_success_is_unhealthy(dash, status):
    # 404 included: a missing prefix in an existing bucket lists as 200 [],
    # so a 404 means the bucket itself is missing or misconfigured.
    statuses, detail, calls = _health_twice(dash, status)
    assert statuses == [503, 503] and calls == 1
    assert f"HTTP {status}" in detail


@pytest.mark.parametrize("exc, shown", [
    (httpx.ReadTimeout(""), "ReadTimeout"),            # empty message
    (httpx.ConnectError("connection refused"), "ConnectError: connection refused"),
    (RuntimeError("boom"), "RuntimeError: boom"),
])
def test_exceptions_are_unhealthy_even_with_an_empty_message(dash, exc, shown):
    statuses, detail, calls = _health_twice(dash, exc)
    assert statuses == [503, 503] and calls == 1
    assert detail.endswith(shown)
