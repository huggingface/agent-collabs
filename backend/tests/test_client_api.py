"""GET /v1/watch.sh — self-distribution of clients/collab_watch.sh.

The script is served from disk rather than baked in, so this also guards the
packaging: if a redeploy forgets to ship clients/ next to app/, the route 404s
and this test is what catches it.
"""
from __future__ import annotations

import subprocess


def test_watch_script_served(env, tmp_path):
    resp = env.client.get("/v1/watch.sh")
    assert resp.status_code == 200
    assert "text/x-shellscript" in resp.headers["content-type"]

    body = resp.text
    assert body.startswith("#!/bin/sh")
    assert "wait=" in body

    # Served bytes must be a syntactically valid POSIX script — serving a broken
    # watcher is worse than serving none, since agents pipe this straight to sh.
    script = tmp_path / "watch.sh"
    script.write_text(body)
    check = subprocess.run(
        ["sh", "-n", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert check.returncode == 0, check.stderr


def test_watch_script_is_served_verbatim(env):
    """Byte-for-byte the file on disk: the bootstrap one-liner pipes this into a
    file and runs it, so any transformation in flight is a bug."""
    from app.routes.client import _SCRIPT_PATH

    resp = env.client.get("/v1/watch.sh")
    assert resp.content == _SCRIPT_PATH.read_bytes()
