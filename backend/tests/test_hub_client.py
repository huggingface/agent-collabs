"""HubClient's read contract against a stubbed huggingface_hub: a listing or
batch download either completes or raises — never a silent subset."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from huggingface_hub.errors import HfHubHTTPError

from app import hub as hub_mod
from app.config import Settings
from app.hub import DownloadFailed, HubClient, ListingFailed


def _client() -> HubClient:
    return HubClient(
        Settings(
            HF_TOKEN="test-token",
            ORG="test-org",
            COLLAB_SLUG="test",
            AUDIT_BUCKET="auditor/test-audit",
        )
    )


def _http_error() -> HfHubHTTPError:
    request = httpx.Request("GET", "https://fake-hub.test/tree")
    return HfHubHTTPError("page 3 failed", response=httpx.Response(500, request=request))


def _entry(path: str):
    return SimpleNamespace(type="file", path=path, size=1, xet_hash=path)


def test_listing_interrupted_mid_pagination_raises(monkeypatch):
    def tree(**_kw):
        yield _entry("message_board/a.md")
        yield _entry("message_board/b.md")
        raise _http_error()

    monkeypatch.setattr(hub_mod, "list_bucket_tree", tree)
    with pytest.raises(ListingFailed):
        _client().list_central_dir("message_board")
    # A scratch listing fails the same way: [] would pass for "no files".
    with pytest.raises(ListingFailed):
        _client().list_bucket_dir("test-org/test-agent-1", "message_board")


def test_download_many_skips_only_genuinely_missing_files(monkeypatch):
    def download(bucket_id, files, raise_on_missing_files, token):
        assert raise_on_missing_files is False  # missing files are skipped, not raised
        for remote, local in files:
            if remote != "gone.md":
                Path(local).write_bytes(remote.encode())

    monkeypatch.setattr(hub_mod, "download_bucket_files", download)
    assert _client().download_many("b", ["a.md", "gone.md"]) == {"a.md": b"a.md"}


def test_download_many_retries_once_then_raises(monkeypatch):
    calls = []

    def flaky(bucket_id, files, raise_on_missing_files, token):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error()
        for remote, local in files:
            Path(local).write_bytes(b"x")

    monkeypatch.setattr(hub_mod, "download_bucket_files", flaky)
    assert _client().download_many("b", ["a.md"]) == {"a.md": b"x"}

    def down(**_kw):
        raise _http_error()

    monkeypatch.setattr(hub_mod, "download_bucket_files", down)
    with pytest.raises(DownloadFailed):
        _client().download_many("b", ["a.md"])
