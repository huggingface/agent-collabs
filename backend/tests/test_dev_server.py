"""The local dev server (scripts/dev_server.py) wires every dependency from its
own Settings. Run in a subprocess: it sets dev env defaults at import time and
replaces the app's dependency overrides, which must not leak into other tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# Starts the dev server with uvicorn.run intercepted, then drives the app with
# a TestClient: an organizer creates a channel, and an agent hits its per-hour
# creation cap (one limiter for the process, not one per request).
SCRIPT = r"""
import json, sys
from unittest.mock import patch
from fastapi.testclient import TestClient
from scripts import dev_server
from app.deps import get_hub, get_settings_dep

def run(app, **kwargs):
    hub = app.dependency_overrides[get_hub]()
    settings = app.dependency_overrides[get_settings_dep]()
    bucket = settings.agent_bucket("byte-bandit")
    out = {}
    with TestClient(app, raise_server_exceptions=False) as client:
        out["organizer"] = client.post(
            "/v1/channels", headers={"Authorization": "Bearer dev-token"},
            json={"name": "dev-room", "agent_id": "human-tester", "body": "Dev theme"},
        ).status_code
        agent = []
        for name in ("one", "two", "three"):
            hub.seed(f"drafts/{name}.md", f"Theme for {name}.", bucket=bucket)
            agent.append(client.post("/v1/channels", json={
                "name": name, "source": f"hf://buckets/{bucket}/drafts/{name}.md",
            }).status_code)
        out["agent"] = agent
    print("RESULT " + json.dumps(out))

with patch.object(sys, "argv", ["dev_server.py", "--root", sys.argv[1], "--seed"]), \
        patch("uvicorn.run", run):
    dev_server.main()
"""


def test_dev_server_channel_creation_and_per_agent_cap(tmp_path):
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("ORG", "COLLAB_SLUG", "AUDIT_BUCKET", "CHANNEL_CREATE_PER_HOUR")
    }
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT, str(tmp_path)],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120,
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    result = json.loads(line[len("RESULT "):])
    assert result["organizer"] == 201
    assert result["agent"] == [201, 201, 429]  # CHANNEL_CREATE_PER_HOUR defaults to 2
