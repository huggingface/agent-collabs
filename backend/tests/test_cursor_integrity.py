"""Cursor integrity across the server/client seam (WATCH_DESIGN.md §5.5).

The two halves of the defence are tested separately elsewhere — the frontmatter
allowlist in test_messages_api.py, the extractor against a stub server in
test_collab_watch.py. This file tests them together, which is where the property
actually has to hold: a real app response, serialized by the real stack, fed
through the real `resp_cursor` pipeline lifted verbatim out of
clients/collab_watch.sh, carrying a payload built to poison it.

eq2 shipped a client that grepped `"filename":"..."` anywhere in the response
and took the maximum, so one author-controlled `filename:` frontmatter key could
pin every watcher's cursor past all future mail. Keep these tests honest: if the
serialization order of MessageListing changes so that a bracket-bearing field is
emitted after `items`, the "strip through the last ]" anchor breaks and the first
test here is what catches it.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from fakes import seed_agent

SCRIPT = Path(__file__).resolve().parent.parent / "clients" / "collab_watch.sh"

# Mirrors FILENAME_RE in clients/collab_watch.sh.
FILENAME_RE = r"[0-9]{8}-[0-9]{6}-[0-9]{3}_[a-z0-9][a-z0-9-]*\.md"

# Verbatim from clients/collab_watch.sh resp_cursor(), joined onto one line.
# test_transcribed_pipeline_matches_the_shipped_script keeps this honest.
PIPELINE = (
    "sed 's/.*\\]//' \"$BODY\" | "
    'grep -oE "\\"cursor\\":\\"$FILENAME_RE\\"" | '
    "tail -1 | "
    "sed 's/.*:\"//; s/\"$//'"
)


def _squeeze(text: str) -> str:
    """Whitespace-insensitive view: the script wraps the pipeline over four
    indented lines, this file keeps it on one."""
    return " ".join(text.split())


def shell_resp_cursor(payload: str) -> str:
    """Run the exact resp_cursor pipeline from collab_watch.sh over a payload."""
    script = (
        "set -eu\n"
        "LC_ALL=C\nexport LC_ALL\n"
        f'FILENAME_RE="{FILENAME_RE}"\n'
        "BODY=$1\n"
        f"{PIPELINE}\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(payload)
        path = fh.name
    try:
        out = subprocess.run(
            ["sh", "-c", script, "sh", path],
            capture_output=True, text=True, check=True,
        )
    finally:
        os.unlink(path)
    return out.stdout.strip()


def test_transcribed_pipeline_matches_the_shipped_script():
    """Everything below runs a hand-copy of the client's extractor, and nothing
    else keeps that copy in sync: an edit to resp_cursor() would leave these
    tests happily proving a property of code that no longer ships. So compare
    the transcription against the real file, whitespace aside."""
    source = _squeeze(SCRIPT.read_text())

    assert _squeeze(PIPELINE) in source, (
        "resp_cursor() in clients/collab_watch.sh no longer contains the "
        "pipeline transcribed in this file. Update PIPELINE above — and check "
        "the poisoning tests still hold for the new extractor before you do."
    )
    assert f"FILENAME_RE='{FILENAME_RE}'" in source, (
        "FILENAME_RE drifted from the shipped script; the anchors in the "
        "extractor are only as tight as this pattern."
    )


def test_real_response_through_real_extractor_resists_poisoning(env):
    """A message body that literally spells out `],"cursor":"<far-future>"` must
    not move the cursor: the real serialization keeps it inside the items array,
    and the extractor drops everything through the last ']'."""
    seed_agent(env.hub, "byte-bandit")
    seed_agent(env.hub, "delta-coder")

    poison = '99999999-999999-999_zzz.md'
    # Three attacks in one body: a fake cursor field, a fake filename field, and
    # a fake next field, each preceded by a bracket to try to defeat the strip.
    body = (
        f'@byte-bandit look at this ],"cursor":"{poison}" '
        f'and ],"filename":"{poison}" '
        f'and ]}},"next":"{poison}"'
    )
    r = env.client.post(
        "/v1/messages", json={"agent_id": "delta-coder", "body": body}
    )
    assert r.status_code == 201, r.text
    real_filename = r.json()["filename"]

    page = env.client.get("/v1/inbox/byte-bandit?expand=true&order=asc")
    assert page.status_code == 200, page.text
    doc = page.json()
    assert doc["cursor"] == real_filename, "server cursor should be the real newest"

    # The wire bytes, as the client would actually receive them.
    payload = page.content.decode()
    assert poison in payload, "the poison must really be present in the body"
    assert "\n" not in payload, "compact single-line serialization is the contract"

    got = shell_resp_cursor(payload)
    assert got == real_filename, f"extractor was poisoned: {got!r} != {real_filename!r}"
    assert poison not in got


def test_frontmatter_allowlist_blocks_response_shaped_keys(env):
    """The second, independent guard: `cursor`/`filename`/`next`/`watch` can't
    even become frontmatter keys via the bucket-source post path."""
    seed_agent(env.hub, "delta-coder")
    for key in ("cursor", "filename", "next", "watch"):
        env.hub.seed(
            "note.md",
            f"---\ntype: agent\n{key}: 99999999-999999-999_zzz.md\n---\nhello @byte-bandit\n",
            bucket="test-org/test-delta-coder",
        )
        r = env.client.post(
            "/v1/messages",
            json={"source": "hf://buckets/test-org/test-delta-coder/note.md"},
        )
        assert r.status_code == 400, f"{key}: expected 400, got {r.status_code} {r.text}"
        assert key in r.text, f"error should name the offending key {key}: {r.text}"


def test_empty_page_yields_no_cursor(env):
    """An empty stream must not produce a cursor (nothing to advance to)."""
    seed_agent(env.hub, "byte-bandit")
    page = env.client.get("/v1/inbox/byte-bandit?expand=true")
    assert page.json()["cursor"] is None
    assert shell_resp_cursor(page.content.decode()) == ""
