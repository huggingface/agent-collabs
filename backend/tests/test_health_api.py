"""/v1/healthz: the warm flag and the read-model gauges."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.main import WARM_FOLDERS, app
from fakes import seed_agent, seed_message


def test_healthz_is_200_and_cold_until_warm_up_completes(env):
    seed_agent(env.hub, "agent-1")
    r = env.client.get("/v1/healthz")
    assert r.status_code == 200 and r.json()["warm"] is False
    env.read_model.warm_up(WARM_FOLDERS)
    assert env.client.get("/v1/healthz").json()["warm"] is True


def test_warm_up_fills_the_caches(env):
    seed_agent(env.hub, "agent-1")
    seed_message(env.hub, "20260601-100000-000", "agent-1", "hi")
    env.read_model.warm_up(WARM_FOLDERS)
    listed, downloaded = env.hub.list_calls, env.hub.download_calls
    assert env.client.get("/v1/messages?expand=true").json()["count"] == 1
    assert (env.hub.list_calls, env.hub.download_calls) == (listed, downloaded)


def test_warm_up_retries_a_failing_folder_before_reporting_warm(env):
    seed_agent(env.hub, "agent-1")
    env.hub.fail_next_listing("agents")
    env.read_model.warm_up(WARM_FOLDERS, retry_s=0)
    assert env.read_model.warm is True
    assert env.read_model.registered_agents() == {"agent-1"}


def test_lifespan_warms_the_read_model_in_the_background(env):
    seed_agent(env.hub, "agent-1")
    with TestClient(app) as client:  # runs the lifespan
        deadline = time.monotonic() + 5
        while not client.get("/v1/healthz").json()["warm"]:
            assert time.monotonic() < deadline, "warm-up never completed"
            time.sleep(0.01)


def test_healthz_read_model_gauges(make_env):
    env = make_env(LISTING_TTL_S=0)
    seed_message(env.hub, "20260601-100000-000", "agent-1", "hi")
    env.client.get("/v1/messages?expand=true")
    gauges = env.client.get("/v1/healthz").json()["read_model"]
    assert gauges["folders"] >= 1
    assert gauges["content_cache_bytes"] > 0
    assert gauges["listing_errors"] == {}

    # A warm folder whose listing fails is served from cache — and named here.
    env.hub.fail_next_listing("message_board")
    assert env.client.get("/v1/messages").json()["count"] == 1
    errors = env.client.get("/v1/healthz").json()["read_model"]["listing_errors"]
    assert set(errors) == {"message_board"}
    assert errors["message_board"]["error"] == "type=HfHubHTTPError status=500"
    assert errors["message_board"]["age_s"] >= 0

    env.client.get("/v1/messages")  # the next listing succeeds and clears it
    assert env.client.get("/v1/healthz").json()["read_model"]["listing_errors"] == {}
