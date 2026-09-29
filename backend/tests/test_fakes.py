"""Tests for FakeHub's chaos-injection toggles: each one fires once, on the
next matching call, then resets to normal behaviour."""
import pytest
from huggingface_hub.errors import HfHubHTTPError

from app.config import Settings
from fakes import FakeHub, seed_message


def make_hub(**overrides) -> FakeHub:
    settings = Settings(
        HF_TOKEN="test-token",
        ORG="test-org",
        COLLAB_SLUG="test",
        AUDIT_BUCKET="auditor/test-audit",
        **overrides,
    )
    return FakeHub(settings)


def test_fail_next_write_fires_once_then_resets():
    hub = make_hub()
    hub.fail_next_write()
    with pytest.raises(HfHubHTTPError):
        hub.write_text_central("agents/a.md", "hi")
    assert "agents/a.md" not in hub._central()

    hub.write_text_central("agents/a.md", "hi")  # toggle reset: this succeeds
    assert hub._central()["agents/a.md"] == b"hi"


def test_fail_next_read_matches_substring_then_resets():
    hub = make_hub()
    seed_message(hub, "20260601-100000-000", "agent-1", "hello")
    path = "message_board/20260601-100000-000_agent-1.md"

    hub.fail_next_read(path_substring="results/")  # doesn't match this path
    assert hub.read_central_text(path)  # passes through untouched

    hub.fail_next_read(path_substring="message_board/")
    with pytest.raises(FileNotFoundError):
        hub.read_central_text(path)

    assert hub.read_central_text(path)  # toggle reset: this succeeds


def test_partial_listing_drops_last_n_then_resets():
    hub = make_hub()
    for i in range(3):
        seed_message(hub, f"2026060{i + 1}-100000-000", "agent-1", f"msg {i}")

    hub.partial_listing("message_board", drop=1)
    assert len(hub.list_central_dir("message_board")) == 2

    assert len(hub.list_central_dir("message_board")) == 3  # toggle reset


def test_fail_next_listing_returns_empty_then_resets():
    hub = make_hub()
    seed_message(hub, "20260601-100000-000", "agent-1", "hello")

    hub.fail_next_listing("message_board")
    assert hub.list_central_dir("message_board") == []

    assert len(hub.list_central_dir("message_board")) == 1  # toggle reset
