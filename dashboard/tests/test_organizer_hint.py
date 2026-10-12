"""The organizer display hint from the backend's GET /v1/me: only a definite
true/false replaces it; an unknown role (null) or a failure keeps it."""
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
    d.BACKEND_API_URL = "https://backend.test"
    return d


class _Client:
    def __init__(self, status, body=None):
        self.status, self.body = status, body

    async def get(self, url, **kw):
        return httpx.Response(self.status, json=self.body, request=httpx.Request("GET", url))


@pytest.mark.parametrize(
    "status, body, hint",
    [
        (200, {"is_organizer": True}, True),
        (200, {"is_organizer": False}, False),
        (200, {"is_organizer": None}, None),  # role unknown: keep the hint
        (200, {}, None),
        (503, {"error": {}}, None),
    ],
)
def test_only_a_definite_answer_sets_the_hint(dash, status, body, hint):
    dash.app.state.plain_client = _Client(status, body)
    assert asyncio.run(dash._fetch_is_organizer("oauth-token")) is hint
