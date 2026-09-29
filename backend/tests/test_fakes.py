"""Tests for FakeHub's chaos-injection toggles: each one fires once, on the
next matching call, then resets to normal behaviour."""
import pytest
from huggingface_hub.errors import HfHubHTTPError

from app.config import Settings
from app.hub import DownloadFailed, ListingFailed
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


@pytest.mark.parametrize("read", ["optional", "text_optional", "audit"])
def test_fail_next_read_propagates_from_optional_reads(read):
    """Optional reads tell missing (None) from failed, so the injected Hub
    error propagates instead of reading as an absent file."""
    hub = make_hub()
    hub.seed("results/verification_status.json", "{}")
    hub.seed("results/run1.md", "x", bucket="test-org/test-agent-1")
    hub.seed("results/quota.jsonl", "{}", bucket=hub._settings.audit_bucket)
    calls = {
        "optional": lambda: hub.read_central_bytes_optional("results/verification_status.json"),
        "text_optional": lambda: hub.read_text_optional(
            "hf://buckets/test-org/test-agent-1/results/run1.md"
        ),
        "audit": lambda: hub.read_audit_bytes("results/quota.jsonl"),
    }

    hub.fail_next_read("message_board/")  # doesn't match this path
    assert calls[read]() is not None
    hub.fail_next_read("results/")
    with pytest.raises(HfHubHTTPError):
        calls[read]()
    assert calls[read]() is not None  # toggle reset

    hub.fail_next_read("results/", RuntimeError("injected"))
    with pytest.raises(RuntimeError, match="injected"):
        calls[read]()


def test_fail_next_read_flattens_hub_errors_to_missing_in_source_reads():
    hub = make_hub()
    uri = "hf://buckets/test-org/test-agent-1/results/run1.md"
    hub.seed("results/run1.md", "x", bucket="test-org/test-agent-1")

    hub.fail_next_read("results/")
    with pytest.raises(FileNotFoundError):
        hub.read_text(uri)
    assert hub.read_text(uri) == "x"  # toggle reset

    hub.fail_next_read("results/", RuntimeError("injected"))
    with pytest.raises(RuntimeError, match="injected"):
        hub.read_text(uri)


def test_fail_next_read_fails_the_whole_download_many_batch():
    """HubClient.download_many raises DownloadFailed once its retry is spent,
    never a subset of the batch."""
    hub = make_hub()
    paths = []
    for i in range(2):
        seed_message(hub, f"2026060{i + 1}-100000-000", "agent-1", f"msg {i}")
        paths.append(f"message_board/2026060{i + 1}-100000-000_agent-1.md")
    central = hub._settings.central_bucket

    hub.fail_next_read("results/")  # matches none of the batch
    assert len(hub.download_many(central, paths)) == 2
    hub.fail_next_read(paths[1])
    with pytest.raises(DownloadFailed):
        hub.download_many(central, paths)
    assert len(hub.download_many(central, paths)) == 2  # toggle reset

    injected = RuntimeError("injected")
    hub.fail_next_read("message_board/", injected)
    with pytest.raises(DownloadFailed) as caught:
        hub.download_many(central, paths)
    assert caught.value.__cause__ is injected


def test_fail_next_read_is_not_consumed_by_an_empty_batch():
    hub = make_hub()
    seed_message(hub, "20260601-100000-000", "agent-1", "hello")

    hub.fail_next_read()
    assert hub.download_many(hub._settings.central_bucket, []) == {}
    with pytest.raises(FileNotFoundError):
        hub.read_central_text("message_board/20260601-100000-000_agent-1.md")


def test_latency_applies_to_every_read(monkeypatch):
    import fakes

    hub = make_hub()
    hub.latency_s = 0.5
    sleeps = []
    monkeypatch.setattr(fakes.time, "sleep", sleeps.append)
    hub.seed("results/run1.md", "x", bucket="test-org/test-agent-1")
    uri = "hf://buckets/test-org/test-agent-1/results/run1.md"

    hub.read_central_bytes_optional("results/x.json")
    hub.read_text_optional(uri)
    hub.read_audit_bytes("results/quota.jsonl")
    hub.download_many(hub._settings.central_bucket, ["results/x.json"])
    hub.read_text(uri)
    assert sleeps == [0.5] * 5


@pytest.mark.parametrize("drop", [1, 3, 4, 5])
def test_partial_listing_raises_instead_of_returning_a_prefix_then_resets(drop):
    hub = make_hub()
    for i in range(3):
        seed_message(hub, f"2026060{i + 1}-100000-000", "agent-1", f"msg {i}")

    hub.partial_listing("message_board", drop=drop)
    with pytest.raises(ListingFailed):
        hub.list_central_dir("message_board")

    assert len(hub.list_central_dir("message_board")) == 3  # toggle reset


def test_partial_listing_dropping_nothing_is_the_full_listing():
    hub = make_hub()
    for i in range(3):
        seed_message(hub, f"2026060{i + 1}-100000-000", "agent-1", f"msg {i}")
    hub.partial_listing("message_board", drop=0)
    assert len(hub.list_central_dir("message_board")) == 3


def test_fail_next_listing_raises_then_resets():
    hub = make_hub()
    seed_message(hub, "20260601-100000-000", "agent-1", "hello")

    hub.fail_next_listing("message_board")
    with pytest.raises(ListingFailed):
        hub.list_central_dir("message_board")

    assert len(hub.list_central_dir("message_board")) == 1  # toggle reset
