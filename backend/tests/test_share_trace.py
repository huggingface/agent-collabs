"""Env-anchored session detection in the share-trace client.

The bug this guards against: with multiple agents working from one directory,
`detect(cwd, "auto")` used to pick by cwd+recency, Claude-Code-first — so a Codex
agent would upload a co-located Claude Code log. Detection now keys on the
harness that INVOKES the script (its env), so agents are never cross-attributed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

# The client is a standalone single file under clients/, not on the test path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "clients"))
import share_trace as st  # noqa: E402


CWD = "/work/proj"  # absolute; detect only uses it to compute slugs / cwd-match
_MARKERS = ("CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def _slug(cwd: str) -> str:
    return re.sub(r"[/._]", "-", os.path.abspath(cwd))


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.home() → tmp
    for v in _MARKERS:
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def _cc(home: Path, sid: str, *, mtime: float | None = None) -> Path:
    d = home / ".claude" / "projects" / _slug(CWD)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    if mtime is not None:
        os.utime(f, (mtime, mtime))
    return f


def _codex(home: Path, *, cwd: str = CWD) -> Path:
    d = home / ".codex" / "sessions" / "2026" / "06" / "26"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "rollout-2026-06-26T00-00-00-abc.jsonl"
    f.write_text(json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n")
    return f


def test_cc_pins_invoking_session_not_newest(home, monkeypatch):
    # CLAUDE_CODE_SESSION_ID must win over the newest-mtime heuristic.
    mine = _cc(home, "mine-sid", mtime=time.time() - 100)
    _cc(home, "other-sid", mtime=time.time())  # newer; would win by recency
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "mine-sid")
    monkeypatch.setenv("CLAUDECODE", "1")
    harness, path, uncertain = st.detect(CWD, "auto")
    assert harness == "claude-code"
    assert path == mine          # the invoking session, not "other-sid"
    assert uncertain is False     # exact pin → no confirmation needed


def test_codex_marker_never_grabs_a_claude_log(home, monkeypatch):
    # The reported bug: a Codex agent in a dir that ALSO has a Claude Code session.
    _cc(home, "cc-sid")                  # co-located CC log (the trap)
    rollout = _codex(home)
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    harness, path, _ = st.detect(CWD, "auto")
    assert harness == "codex"
    assert path == rollout               # never the CC log


def test_ambiguous_without_markers_refuses(home):
    # No env marker + both harnesses present → refuse rather than misattribute.
    _cc(home, "cc-sid")
    _codex(home)
    with pytest.raises(SystemExit):
        st.detect(CWD, "auto")


def test_no_marker_single_harness_is_used(home):
    rollout = _codex(home)               # only Codex present, no markers
    harness, path, _ = st.detect(CWD, "auto")
    assert harness == "codex" and path == rollout


def test_missing_session_id_refuses_to_guess(home, monkeypatch):
    # If Claude exposes an exact session id, selecting any other log is unsafe.
    _cc(home, "real-sid")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ghost-sid")
    monkeypatch.setenv("CLAUDECODE", "1")
    with pytest.raises(SystemExit) as exc:
        st.detect(CWD, "auto")
    assert "ghost-sid" in str(exc.value)
    assert "refusing to guess" in str(exc.value)


def test_explicit_transcript_dry_run_works(home, monkeypatch, tmp_path, capsys):
    # Explicit transcript mode is the strongest way to pin a session; it must not
    # require auto-detection's `uncertain` return value.
    transcript = tmp_path / "rollout-2026-06-29T00-00-00-explicit.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-06-29T00:00:00Z",
                        "payload": {"session_id": "explicit-sess", "model": "gpt-test"},
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": "2026-06-29T00:01:00Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": 1,
                                    "output_tokens": 2,
                                    "cached_input_tokens": 0,
                                    "reasoning_output_tokens": 0,
                                    "total_tokens": 3,
                                }
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "timestamp": "2026-06-29T00:02:00Z",
                        "payload": {"type": "local_shell_call", "call_id": "c1"},
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "share_trace.py",
            "--transcript",
            str(transcript),
            "--harness",
            "codex",
            "--dry-run",
        ],
    )
    assert st.main() == 0
    out = capsys.readouterr().out
    assert "session    : explicit-sess" in out
    assert "tokens     : 3" in out
