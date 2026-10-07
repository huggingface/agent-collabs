"""Caller-token bucket writes run in a child process (app/caller_write.py), so
a refused Xet upload cannot poison the backend's own Xet session."""
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from huggingface_hub.errors import HfHubHTTPError

import app.caller_write as caller_write
import app.hub as hub_module

TOKEN = "hf_CALLERsecretTokenDoNotLeak42"
ADMIN = "hf_ADMINsecretTokenDoNotLeak42"
BUCKET = "test-org/test-agent-9"
BACKEND_DIR = Path(caller_write.__file__).resolve().parent.parent

# Stand-in child: behaves according to the bucket name, and reports what it
# could see of the token and the environment.
FAKE_CHILD = r"""
import json, os, sys, time
req = json.loads(sys.stdin.read())
seen = {"token_on_stdin": req["token"] == %r,
        "token_in_argv": any(%r in a for a in sys.argv),
        "admin_in_env": any(%r in v for v in os.environ.values()),
        "hf_token_path": os.environ.get("HF_TOKEN_PATH")}
kind = req["bucket"].split("/")[1]
if kind == "slow":
    time.sleep(30)
if kind == "crash":
    sys.stderr.write("Traceback ... token=" + req["token"] + "\n")
    sys.exit(1)
if kind == "garbage":
    print("not json")
    sys.exit(0)
result = {"ok": {"result": "ok"},
          "forbid": {"result": "forbidden", "status": 403},
          "fail": {"result": "failed", "type": "RuntimeError", "status": None}}.get(kind, {"result": "ok"})
result["seen"] = seen
print(json.dumps(result))
""" % (TOKEN, TOKEN, ADMIN)


@pytest.fixture
def client(env, monkeypatch):
    """A real HubClient whose child is the stand-in above, with the pre-check
    neutral and in-process uploads forbidden outright."""
    monkeypatch.setenv("HF_TOKEN", ADMIN)
    monkeypatch.setattr(hub_module, "_CALLER_WRITE_CMD", [sys.executable, "-c", FAKE_CHILD])

    def no_in_process_upload(**k):
        raise AssertionError("caller write ran in the backend process")

    monkeypatch.setattr(hub_module, "batch_bucket_files", no_in_process_upload)
    c = hub_module.HubClient(env.settings)
    monkeypatch.setattr(c, "caller_may_write", lambda bucket, token: None)
    return c


def _hub_error(status):
    resp = httpx.Response(status, request=httpx.Request("GET", "https://hf.co/api/x"))
    return HfHubHTTPError(f"{status}", response=resp)


# ── classification (shared by the child and create_bucket_as) ────────────────

@pytest.mark.parametrize("exc, expected", [
    (_hub_error(403), {"result": "forbidden", "status": 403}),
    (_hub_error(401), {"result": "forbidden", "status": 401}),
    (ConnectionError("Network error: Request error: HTTP status client error (403 Forbidden), "
                     "domain: https://huggingface.co/api/buckets/o/b/xet-write-token"),
     {"result": "forbidden", "status": 403}),
    # A bare 401/403 elsewhere in the text (a bucket name, a URL) is not a refusal.
    (ConnectionError("Network error: connection reset, domain: https://huggingface.co/api/buckets/o/dev-403/x"),
     {"result": "failed", "type": "ConnectionError", "status": None}),
    (ConnectionError("HTTP status client error (404 Not Found)"),
     {"result": "failed", "type": "ConnectionError", "status": None}),
    (RuntimeError("Previous task error: HTTP status client error (403 Forbidden)"),
     {"result": "failed", "type": "RuntimeError", "status": None}),
    (_hub_error(500), {"result": "failed", "type": "HfHubHTTPError", "status": 500}),
])
def test_classify(exc, expected):
    assert caller_write.classify(exc) == expected


def test_child_run_reports_structured_results_without_the_token(monkeypatch):
    import huggingface_hub

    calls = []
    outcomes = iter([None, _hub_error(403), RuntimeError("boom " + TOKEN)])

    def fake_upload(**k):
        calls.append(k)
        e = next(outcomes)
        if e:
            raise e

    monkeypatch.setattr(huggingface_hub, "batch_bucket_files", fake_upload)
    req = {"token": TOKEN, "bucket": BUCKET, "path": ".bucket-sync-handshake", "text": "u"}
    results = [caller_write.run(req) for _ in range(3)]
    assert [r["result"] for r in results] == ["ok", "forbidden", "failed"]
    assert calls[0]["token"] == TOKEN and calls[0]["add"] == [(b"u", ".bucket-sync-handshake")]
    assert TOKEN not in json.dumps(results)


def test_real_child_runs_isolated_and_returns_a_result(monkeypatch):
    # The real module end to end, with no network: an invalid bucket id is
    # rejected by huggingface_hub before any request (a closed port instead
    # would sit in its retry backoff for minutes). The token goes in on stdin
    # and must come back out nowhere.
    env = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN",)}
    env.update(HF_ENDPOINT="http://127.0.0.1:9", HF_TOKEN_PATH=os.devnull, HF_HUB_DISABLE_PROGRESS_BARS="1")
    proc = subprocess.run(
        [sys.executable, "-m", "app.caller_write"],
        input=json.dumps({"token": TOKEN, "bucket": "not a/valid bucket id", "path": "p", "text": "t"}),
        capture_output=True, text=True, timeout=120, cwd=BACKEND_DIR, env=env,
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["result"] == "failed"
    assert TOKEN not in proc.stdout and TOKEN not in proc.stderr
    bad = subprocess.run([sys.executable, "-m", "app.caller_write"], input="{", capture_output=True,
                         text=True, timeout=60, cwd=BACKEND_DIR, env=env)
    assert json.loads(bad.stdout)["type"] == "BadRequest"


# ── parent: HubClient.write_text_as ──────────────────────────────────────────

def _seen_by_child(monkeypatch):
    seen = {}
    real = hub_module._run_caller_write

    def spy(*a):
        r = real(*a)
        seen.update(r.get("seen", {}))
        return r

    monkeypatch.setattr(hub_module, "_run_caller_write", spy)
    return seen


def test_ok_write_uses_stdin_token_and_no_admin_credential(client, monkeypatch):
    seen = _seen_by_child(monkeypatch)
    client.write_text_as("test-org/ok", ".bucket-sync-handshake", "u", TOKEN)
    assert seen == {"token_on_stdin": True, "token_in_argv": False,
                    "admin_in_env": False, "hf_token_path": os.devnull}


def test_refused_write_is_permission_error(client):
    with pytest.raises(PermissionError) as exc:
        client.write_text_as("test-org/forbid", "p", "t", TOKEN)
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("kind", ["fail", "crash", "garbage"])
def test_failed_crashed_or_unreadable_child_is_upstream_error(client, caplog, kind):
    caplog.set_level("DEBUG")
    with pytest.raises(hub_module.HubUnreachable) as exc:
        client.write_text_as(f"test-org/{kind}", "p", "t", TOKEN)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text  # the crashing child printed it to stderr


def test_child_timeout_is_upstream_error(client, monkeypatch):
    monkeypatch.setattr(hub_module, "CALLER_WRITE_TIMEOUT_S", 1.0)
    t0 = time.monotonic()
    with pytest.raises(hub_module.HubUnreachable):
        client.write_text_as("test-org/slow", "p", "t", TOKEN)
    assert time.monotonic() - t0 < 15


def test_concurrent_writes_are_bounded(client, monkeypatch):
    monkeypatch.setattr(hub_module, "CALLER_WRITE_SLOTS", threading.BoundedSemaphore(1))
    monkeypatch.setattr(hub_module, "CALLER_WRITE_TIMEOUT_S", 5.0)
    monkeypatch.setattr(hub_module, "CALLER_WRITE_SLOT_WAIT_S", 0.5)
    slow = threading.Thread(target=lambda: pytest.raises(
        hub_module.HubUnreachable, client.write_text_as, "test-org/slow", "p", "t", TOKEN))
    slow.start()
    time.sleep(1.0)  # the slow child holds the only slot
    with pytest.raises(hub_module.HubUnreachable):
        client.write_text_as("test-org/ok", "p", "t", TOKEN)
    slow.join()
    client.write_text_as("test-org/ok", "p", "t", TOKEN)  # slot released


def test_precheck_refusal_never_starts_a_child(client, monkeypatch):
    monkeypatch.setattr(client, "caller_may_write", lambda bucket, token: False)

    def no_child(*a):
        raise AssertionError("child started after a refused pre-check")

    monkeypatch.setattr(hub_module, "_run_caller_write", no_child)
    with pytest.raises(PermissionError):
        client.write_text_as("test-org/ok", "p", "t", TOKEN)


def test_precheck_success_then_upload_failure_is_upstream_error(client, monkeypatch):
    monkeypatch.setattr(client, "caller_may_write", lambda bucket, token: True)
    with pytest.raises(hub_module.HubUnreachable):
        client.write_text_as("test-org/fail", "p", "t", TOKEN)
    with pytest.raises(PermissionError):  # permissions changed after the check
        client.write_text_as("test-org/forbid", "p", "t", TOKEN)


class _FakeSession:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return httpx.Response(self.outcome, request=httpx.Request("GET", url))


@pytest.mark.parametrize("outcome, expected", [
    (403, False), (401, False), (200, True), (500, None), (404, None), (httpx.ConnectError("down"), None),
])
def test_caller_may_write(env, monkeypatch, outcome, expected):
    session = _FakeSession(outcome)
    monkeypatch.setattr(hub_module, "get_session", lambda: session)
    c = hub_module.HubClient(env.settings)
    assert c.caller_may_write(BUCKET, TOKEN) is expected
    url, headers = session.calls[0]
    assert url.endswith(f"/api/buckets/{BUCKET}/xet-write-token")
    assert headers["authorization"] == f"Bearer {TOKEN}"
