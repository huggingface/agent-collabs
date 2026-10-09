"""Bootstrap helpers that must fail closed: token resolution, Space-card
stamping, and bucket existence checks."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import init_challenge as b  # noqa: E402


# ── resolve_token ────────────────────────────────────────────────────────────

def _resolve(tmp_path, env_line, cached="hf_cachedToken"):
    (tmp_path / ".env").write_text(env_line + "\n")
    with patch.object(b, "REPO_ROOT", tmp_path), \
            patch.object(b, "get_token", return_value=cached), \
            patch.dict(os.environ, {}, clear=True):
        return b.resolve_token()


@pytest.mark.parametrize("line", ["HF_TOKEN=", "HF_TOKEN=   ", "HF_TOKEN= # no token yet"])
def test_empty_env_token_falls_back_to_cached_login(tmp_path, line):
    assert _resolve(tmp_path, line) == "hf_cachedToken"


@pytest.mark.parametrize("line, expected", [
    ("HF_TOKEN=hf_plain", "hf_plain"),
    ("HF_TOKEN=hf_plain  # comment", "hf_plain"),
    ('HF_TOKEN="hf_quoted"  # comment', "hf_quoted"),
    ("HF_TOKEN='hf_single'", "hf_single"),
])
def test_env_token_beats_cached_login(tmp_path, line, expected):
    assert _resolve(tmp_path, line) == expected


def test_no_token_anywhere_exits(tmp_path):
    with pytest.raises(SystemExit):
        _resolve(tmp_path, "HF_TOKEN=", cached=None)


# ── _stamp ───────────────────────────────────────────────────────────────────

CARD = """---
title: Challenge Dashboard
hf_oauth: true
hf_oauth_authorized_org: REPLACED_BY_BOOTSTRAP
short_description: Live dashboard
---

# Challenge dashboard
"""


def _front(text):
    return yaml.safe_load(text.split("---")[1])


def test_stamps_placeholder_and_already_edited_values():
    once = b._stamp(CARD, "hf_oauth_authorized_org", "acme")
    assert _front(once)["hf_oauth_authorized_org"] == "acme"
    twice = b._stamp(once, "hf_oauth_authorized_org", "other-org")  # re-run after an edit
    assert _front(twice)["hf_oauth_authorized_org"] == "other-org"
    assert twice.endswith("# Challenge dashboard\n")


def test_duplicate_key_in_frontmatter_is_refused():
    card = CARD.replace("hf_oauth: true\n", "hf_oauth: true\nhf_oauth_authorized_org: wrong-org\n")
    with pytest.raises(SystemExit, match="found 2"):
        b._stamp(card, "hf_oauth_authorized_org", "acme")


def test_missing_key_is_refused():
    card = CARD.replace("hf_oauth_authorized_org: REPLACED_BY_BOOTSTRAP\n", "")
    with pytest.raises(SystemExit, match="found 0"):
        b._stamp(card, "hf_oauth_authorized_org", "acme")


def test_key_only_in_the_body_is_refused_and_body_is_never_stamped():
    card = CARD.replace("hf_oauth_authorized_org: REPLACED_BY_BOOTSTRAP\n", "") \
        + "\nhf_oauth_authorized_org: shown as an example\n"
    with pytest.raises(SystemExit, match="found 0"):
        b._stamp(card, "hf_oauth_authorized_org", "acme")
    both = CARD + "\nhf_oauth_authorized_org: shown as an example\n"
    stamped = b._stamp(both, "hf_oauth_authorized_org", "acme")
    assert stamped.endswith("hf_oauth_authorized_org: shown as an example\n")


def test_card_without_frontmatter_is_refused():
    with pytest.raises(SystemExit, match="frontmatter"):
        b._stamp("# just markdown\ntitle: x\n", "title", "y")


def test_real_dashboard_card_stamps_cleanly():
    card = (Path(__file__).resolve().parents[2] / "dashboard" / "README.md").read_text()
    for key in ("hf_oauth_authorized_org", "title", "short_description"):
        card = b._stamp(card, key, '"stamped"')
    fm = _front(card)
    assert fm["hf_oauth_authorized_org"] == fm["title"] == fm["short_description"] == "stamped"


# ── bucket_has ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entries, expected", [
    ([], False), ([SimpleNamespace(path="other.md")], False), ([SimpleNamespace(path="README.md")], True),
])
def test_bucket_has(entries, expected):
    with patch.object(b, "list_bucket_tree", return_value=iter(entries)):
        assert b.bucket_has("org/bucket", "README.md", "tok") is expected


def test_listing_that_fails_midway_aborts_instead_of_guessing():
    def listing(**kwargs):
        yield SimpleNamespace(path="README.md")
        raise RuntimeError("simulated transient read failure")

    with patch.object(b, "list_bucket_tree", side_effect=listing):
        with pytest.raises(SystemExit):
            b.bucket_has("org/bucket", "README.md", "tok")


# ── org namespace comparisons ────────────────────────────────────────────────

@pytest.mark.parametrize("repo_id, org, expected", [
    ("acme/x-audit", "acme", True),
    ("Acme/x-audit", "acme", True),
    ("acme/x-audit", "ACME", True),
    ("acme-admin/x-audit", "acme", False),
    ("Acme-Admin/x-eval", "acme", False),
])
def test_in_org_ignores_case(repo_id, org, expected):
    assert b._in_org(repo_id, org) is expected


@pytest.mark.parametrize("audit_bucket, warned", [
    ("Acme/x-audit", True), ("acme/x-audit", True), ("acme-admin/x-audit", False),
])
def test_private_eval_set_in_challenge_org_is_flagged_whatever_the_case(tmp_path, capsys, audit_bucket, warned):
    cfg = {
        "challenge": {"org": "acme", "slug": "x", "title": "X"},
        "jobs": {"enabled": True},
        "verification": {"mode": "jobs"},
        "storage": {"audit_bucket": audit_bucket},
    }
    path = tmp_path / "challenge.yaml"
    path.write_text(yaml.safe_dump(cfg))
    b.load_config(path)
    assert ("storage.audit_bucket" in capsys.readouterr().out) is warned


# ── wait_healthy ─────────────────────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def _wait(monkeypatch, health, stages, *, token=None, hub_token="hf_bootstrap", errmsg=None):
    """Run wait_healthy on a fake clock. `health`/`stages` are lists consumed
    one per poll (the last value repeats); a stage of Exception means the
    runtime lookup fails. Returns (result, http_calls, runtime_tokens, out)."""
    clock = _Clock()
    monkeypatch.setattr(b.time, "time", clock.time)
    monkeypatch.setattr(b.time, "sleep", clock.sleep)
    http_calls, runtime_tokens = [], []

    def fake_get(url, **kw):
        http_calls.append(kw.get("headers"))
        status = health[min(len(http_calls) - 1, len(health) - 1)]
        if status is Exception:
            raise httpx.ConnectError("down")
        return SimpleNamespace(status_code=status)

    def fake_runtime(repo_id, token=None):
        runtime_tokens.append(token)
        stage = stages[min(len(runtime_tokens) - 1, len(stages) - 1)]
        if stage is Exception:
            raise RuntimeError("runtime lookup failed")
        return SimpleNamespace(stage=stage, raw={"errorMessage": errmsg} if errmsg else {})

    monkeypatch.setattr(b.httpx, "get", fake_get)
    monkeypatch.setattr(b, "get_space_runtime", fake_runtime)
    result = b.wait_healthy("https://x.hf.space", "/health", "acme/x-backend",
                            token=token, hub_token=hub_token, timeout_s=600, poll_s=10)
    return result, http_calls, runtime_tokens


def test_ordinary_startup_becomes_healthy(monkeypatch):
    S = b.SpaceStage
    ok, calls, _ = _wait(monkeypatch, [503, 503, 503, 200], [S.BUILDING, S.RUNNING_BUILDING, S.APP_STARTING])
    assert ok and len(calls) == 4


def test_paused_for_quota_stops_early_with_a_hint(monkeypatch, capsys):
    ok, calls, tokens = _wait(monkeypatch, [503], [b.SpaceStage.PAUSED],
                              errmsg="Quota exceeded for flavor cpu-basic: limit=0")
    out = capsys.readouterr().out
    assert not ok
    assert len(calls) == b.DEAD_STAGE_POLLS          # ~30 s, not the full 600 s
    assert "Quota exceeded" in out and "no Space quota" in out
    assert tokens and all(t == "hf_bootstrap" for t in tokens)


def test_build_error_stops_early_without_a_quota_hint(monkeypatch, capsys):
    ok, calls, _ = _wait(monkeypatch, [Exception], [b.SpaceStage.BUILD_ERROR])
    out = capsys.readouterr().out
    assert not ok and len(calls) == b.DEAD_STAGE_POLLS
    assert "BUILD_ERROR" in out and "see the Space logs" in out and "quota" not in out


def test_stale_error_stage_right_after_upload_is_not_fatal(monkeypatch):
    # The previous BUILD_ERROR lingers for two polls, then the rebuild starts.
    S = b.SpaceStage
    ok, calls, _ = _wait(monkeypatch, [503, 503, 503, 503, 200],
                         [S.BUILD_ERROR, S.BUILD_ERROR, S.BUILDING, S.APP_STARTING, S.RUNNING])
    assert ok and len(calls) == 5


def test_runtime_lookup_failures_fall_back_to_the_timeout(monkeypatch):
    ok, calls, _ = _wait(monkeypatch, [503], [Exception])
    assert not ok and len(calls) == 60


def test_private_space_gets_the_bearer_token_on_its_endpoint(monkeypatch):
    ok, calls, tokens = _wait(monkeypatch, [200], [b.SpaceStage.RUNNING], token="hf_eval", hub_token="hf_bootstrap")
    assert ok and calls[0] == {"Authorization": "Bearer hf_eval"}


# ── channels: agent creation switch ──────────────────────────────────────────

def _readme_cfg(channels):
    cfg = yaml.safe_load((Path(__file__).resolve().parents[2] / "challenge.yaml").read_text())
    cfg["challenge"].update(org="acme", slug="x")
    cfg["storage"] = {"central_bucket": "acme/x-main", "audit_bucket": "acme/x-audit"}
    if channels is None:
        cfg.pop("channels", None)
    else:
        cfg["channels"] = channels
    return cfg


@pytest.mark.parametrize("channels, enabled, per_hour", [
    (None, "true", "2"),  # default: agents may create
    ({"agent_creation": True, "create_per_hour": 5}, "true", "5"),
    ({"agent_creation": False}, "false", "2"),
])
def test_backend_gets_the_channel_creation_settings(channels, enabled, per_hour):
    out = b.backend_variables(_readme_cfg(channels))
    assert out["AGENT_CHANNEL_CREATION"] == enabled
    assert out["CHANNEL_CREATE_PER_HOUR"] == per_hour


def test_readme_matches_the_channel_creation_switch():
    from central_readme import build_central_readme

    on = build_central_readme(_readme_cfg({"agent_creation": True, "create_per_hour": 3}), "https://api", "https://dash")
    assert "Create a channel when a real topic has no home" in on
    assert "At most 3 new channels per agent per hour" in on
    assert "hf://buckets/acme/x-$AGENT_ID/channels/eval-harness.md" in on
    assert "curated by the organizers" not in on

    off = build_central_readme(_readme_cfg({"agent_creation": False}), "https://api", "https://dash")
    assert "curated by the organizers" in off
    assert "Create a channel when a real topic has no home" not in off
