"""Bootstrap helpers that must fail closed: token resolution, Space-card
stamping, and bucket existence checks."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
