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
# Env the client reads; cleared so the host environment never leaks into tests.
_MARKERS = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED",
    "CODEX_HOME", "CLAUDE_CONFIG_DIR", "COLLAB_BACKEND", "API",
)


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


def _codex(home: Path, *, cwd: str = CWD, name: str = "abc", extra: list | None = None) -> Path:
    d = home / ".codex" / "sessions" / "2026" / "06" / "26"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-2026-06-26T00-00-00-{name}.jsonl"
    records = [{"type": "session_meta", "payload": {"cwd": cwd}}, *(extra or [])]
    f.write_text("".join(json.dumps(r) + "\n" for r in records))
    return f


def test_cc_pins_invoking_session_not_newest(home, monkeypatch):
    # CLAUDE_CODE_SESSION_ID must win over the newest-mtime heuristic.
    mine = _cc(home, "mine-sid", mtime=time.time() - 100)
    _cc(home, "other-sid", mtime=time.time())  # newer; would win by recency
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "mine-sid")
    monkeypatch.setenv("CLAUDECODE", "1")
    harness, path = st.detect(CWD, "auto")
    assert harness == "claude-code"
    assert path == mine          # the invoking session, not "other-sid"


def test_codex_marker_never_grabs_a_claude_log(home, monkeypatch):
    # The reported bug: a Codex agent in a dir that ALSO has a Claude Code session.
    _cc(home, "cc-sid")                  # co-located CC log (the trap)
    rollout = _codex(home)
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    harness, path = st.detect(CWD, "auto")
    assert harness == "codex"
    assert path == rollout               # never the CC log


def test_config_dir_env_vars_are_honoured(home, monkeypatch, tmp_path):
    # CLAUDE_CONFIG_DIR / CODEX_HOME relocate the logs; ~/.claude and ~/.codex
    # are only the fallbacks.
    assert st._cc_project_dir(CWD) == home / ".claude" / "projects" / _slug(CWD)
    assert st._codex_home() == home / ".codex"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx" / ".codex"))
    assert st._cc_project_dir(CWD) == tmp_path / "cc" / "projects" / _slug(CWD)
    rollout = _codex(tmp_path / "cx")  # writes under <root>/.codex/sessions
    assert st._codex_logs() == [rollout]
    assert st._infer_harness(rollout) == "codex"


def test_claude_config_dir_detects_pinned_session(home, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    d = tmp_path / "cc" / "projects" / _slug(CWD)
    d.mkdir(parents=True)
    mine = d / "sid-1.jsonl"
    mine.write_text("{}\n")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sid-1")
    assert st.detect(CWD, "auto") == ("claude-code", mine)
    assert st._infer_harness(mine) == "claude-code"


def test_codex_home_is_not_a_harness_marker(home, monkeypatch):
    # CODEX_HOME is often exported globally (e.g. by agent managers); it only
    # relocates the logs and must not decide which harness is running.
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    assert st._running_harness() == (None, None)


def test_codex_matches_only_the_recorded_working_directory(home):
    assert st._codex_matches_cwd(_codex(home, cwd="/work/proj", name="mine"), CWD)
    for other in ("/work/proj2", "/work/proj-old", "/archive/work/proj", "/work/proj@private"):
        assert not st._codex_matches_cwd(_codex(home, cwd=other, name="other"), CWD)
    # Mentioning this directory in a command does not make it the session's cwd.
    mention = {"type": "response_item", "payload": {"command": f"cd {CWD} && ls"}}
    assert not st._codex_matches_cwd(_codex(home, cwd="/elsewhere", name="m", extra=[mention]), CWD)


def test_ambiguous_without_markers_refuses(home):
    # No env marker + both harnesses present → refuse rather than misattribute.
    _cc(home, "cc-sid")
    _codex(home)
    with pytest.raises(SystemExit):
        st.detect(CWD, "auto")


def test_no_marker_single_harness_is_used(home):
    rollout = _codex(home)               # only Codex present, no markers
    harness, path = st.detect(CWD, "auto")
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


def test_redactor_preserves_trace_structure_and_task_context():
    secrets = {
        "hf": "hf_" + "A" * 24,
        "github_classic": "ghp_" + "B" * 24,
        "github_fine": "github_pat_" + "C" * 30,
        "sk": "sk-" + "D" * 24,
        "aws_long_lived": "AKIA" + "E" * 16,
        "aws_temporary": "ASIA" + "F" * 16,
        "slack": "xoxb-" + "1" * 12 + "-" + "G" * 24,
        "gitlab": "glpat-" + "H" * 24,
        "google": "AIza" + "I" * 35,
        "npm": "npm_" + "J" * 36,
        "pypi": "pypi-" + "K" * 24,
        "jwt": "eyJ" + "L" * 12 + "." + "M" * 12 + "." + "N" * 12,
    }
    record = {
        "type": "response_item",
        "payload": {
            "task": "Fix the payment retry while preserving the commit history.",
            "commit": "7f4d3b2a" * 5,
            "headers": {
                "Authorization": "Bearer " + secrets["hf"],
                "Cookie": "session=top-secret-cookie",
            },
            "password": "correct horse battery staple",
            "aws_secret_access_key": "O" * 40,
            "command": (
                "curl -H 'Authorization: Basic dXNlcjpwYXNz' "
                "'https://alice:db-pass@db.internal/app"
                "?access_token=query-secret'"
            ),
            "private_key": (
                "-----BEGIN PRIVATE KEY-----\nsecret-material\n"
                "-----END PRIVATE KEY-----"
            ),
            "provider_values": list(secrets.values()),
        },
    }

    redactor = st.TraceRedactor("secrets")
    result = redactor.redact_jsonl(json.dumps(record) + "\n")
    parsed = json.loads(result)

    assert parsed["type"] == "response_item"
    assert parsed["payload"]["task"] == record["payload"]["task"]
    assert parsed["payload"]["commit"] == record["payload"]["commit"]
    assert "curl -H" in parsed["payload"]["command"]
    assert "db.internal/app" in parsed["payload"]["command"]
    assert all(secret not in result for secret in secrets.values())
    for sensitive in (
        "top-secret-cookie",
        "correct horse battery staple",
        "O" * 40,
        "dXNlcjpwYXNz",
        "alice",
        "db-pass",
        "query-secret",
        "secret-material",
    ):
        assert sensitive not in result
    assert "<REDACTED:BEARER_TOKEN_1>" in result
    assert "<REDACTED:PRIVATE_KEY_" in result
    assert redactor.summary()


def test_balanced_redaction_uses_stable_aliases_and_preserves_relative_paths():
    text = json.dumps(
        {
            "message": (
                "Ask alice@example.com to inspect /Users/alice/work/app.py; "
                "alice@example.com owns src/app.py"
            )
        }
    ) + "\n"
    redactor = st.TraceRedactor("balanced")
    result = redactor.redact_jsonl(text)

    assert result.count("<REDACTED:EMAIL_1>") == 2
    assert "$HOME/work/app.py" in result
    assert "src/app.py" in result
    assert redactor.summary()["EMAIL"] == 2
    assert redactor.summary()["HOME_PATH"] == 1


def test_privacy_levels_are_progressively_stricter():
    text = (
        "Contact alice@example.com under /home/alice/work, then call "
        "https://api.internal.example/v1 from 10.20.30.40"
    )

    secrets = st.redact(text, privacy="secrets")
    balanced = st.redact(text, privacy="balanced")
    strict = st.redact(text, privacy="strict")

    assert "alice@example.com" in secrets
    assert "/home/alice/work" in secrets
    assert "api.internal.example" in secrets
    assert "10.20.30.40" in secrets
    assert "alice@example.com" not in balanced
    assert "$HOME/work" in balanced
    assert "api.internal.example" in balanced
    assert "10.20.30.40" in balanced
    assert "api.internal.example" not in strict
    assert "10.20.30.40" not in strict
    assert "https://<REDACTED:HOST_1>/v1" in strict


def test_escaped_authorization_header_is_redacted_without_losing_command():
    line = json.dumps(
        {
            "command": (
                'curl -H \\"Authorization: Bearer escaped.token-value\\" '
                "https://example.com/v1"
            )
        }
    ) + "\n"
    result = st.redact(line, privacy="secrets")

    assert "escaped.token-value" not in result
    assert "Authorization: Bearer <REDACTED:BEARER_TOKEN_1>" in result
    assert "curl -H" in result
    assert "https://example.com/v1" in result


def test_redaction_is_idempotent_for_quoted_and_unquoted_assignments():
    text = json.dumps(
        {
            "command": "run --password='two words' api_key=opaque-value",
            "password": "structured value",
        }
    ) + "\n"
    first = st.redact(text, privacy="balanced")
    second = st.redact(first, privacy="balanced")

    assert second == first
    assert "two words" not in first
    assert "opaque-value" not in first
    assert "structured value" not in first


def test_custom_patterns_are_stable_and_pattern_file_is_validated(tmp_path):
    patterns = tmp_path / "redact-patterns.txt"
    patterns.write_text("# customer identifiers\nAcme-(?:North|South)\n")
    compiled = st._custom_patterns(str(patterns))
    redactor = st.TraceRedactor("secrets", compiled)

    result = redactor.redact_jsonl(
        json.dumps({"task": "Compare Acme-North with Acme-North and Acme-South"}) + "\n"
    )
    assert result.count("<REDACTED:CUSTOM_1>") == 2
    assert result.count("<REDACTED:CUSTOM_2>") == 1
    assert "Compare " in result

    patterns.write_text("(\n")
    with pytest.raises(SystemExit, match="invalid regex"):
        st._custom_patterns(str(patterns))


def test_full_upload_only_sends_scrubbed_content_and_neutral_filename(
    home, monkeypatch, tmp_path
):
    secret = "github_pat_" + "Z" * 30
    transcript = tmp_path / "alice@example.com.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "timestamp": "2026-06-29T00:00:00Z",
                        "payload": {
                            "session_id": "scrubbed-session",
                            "model": "gpt-test",
                        },
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
                        "payload": {
                            "type": "local_shell_call",
                            "call_id": "c1",
                            "command": (
                                f"deploy with {secret} for alice@example.com "
                                "from /Users/alice/work/app.py"
                            ),
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    uploads = {}

    def capture_upload(local, destination):
        uploads[destination] = Path(local).read_text()

    monkeypatch.setattr(st, "_hf_cp", capture_upload)
    monkeypatch.setattr(st.shutil, "which", lambda _: "/usr/bin/hf")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "share_trace.py",
            "--transcript",
            str(transcript),
            "--harness",
            "codex",
            "--full",
            "--yes",
            "--upload-only",
            "--agent-id",
            "agent-1",
            "--org",
            "test-org",
            "--slug",
            "test-collab",
        ],
    )

    assert st.main() == 0
    manifest_uri = next(uri for uri in uploads if uri.endswith("/manifest.md"))
    trace_uri = next(uri for uri in uploads if uri.endswith("/trace.jsonl"))
    assert "alice@example.com" not in uploads[manifest_uri]
    assert '"privacy": "balanced"' in uploads[manifest_uri]
    redaction = json.loads(uploads[manifest_uri].split("---")[1])["redaction"]
    assert {"category": "GITHUB_TOKEN", "count": 1} in redaction["counts"]
    assert secret not in uploads[trace_uri]
    assert "alice@example.com" not in uploads[trace_uri]
    assert "/Users/alice" not in uploads[trace_uri]
    assert "$HOME/work/app.py" in uploads[trace_uri]
    assert all("alice@example.com.jsonl" not in uri for uri in uploads)


def test_session_id_must_be_a_safe_bucket_component():
    assert st._safe_session_id("rollout-2026.06_29") == "rollout-2026.06_29"
    with pytest.raises(SystemExit, match="safe --session-id"):
        st._safe_session_id("../another-session")


def test_claude_code_usage_is_counted_once_per_message_id(tmp_path):
    # Claude Code writes one line per content block; lines of one API response
    # repeat message.id and usage. Summing every line over-counted tokens.
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 1}

    def line(mid, content):
        msg = {"model": "m", "content": content, "usage": usage}
        if mid:
            msg["id"] = mid
        return json.dumps({"type": "assistant", "sessionId": "s", "message": msg})

    tool = {"type": "tool_use", "id": "tu1", "name": "Bash"}
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("\n".join([
        line("msg_1", [{"type": "thinking"}]),
        line("msg_1", [{"type": "text"}]),
        line("msg_1", [tool]),
        line("msg_1", [tool]),  # same tool_use block written twice
        line("msg_2", [{"type": "tool_use", "id": "tu2", "name": "Read"}]),
        line(None, [{"type": "text"}]),  # no id: still counts once
    ]) + "\n")

    fields = st.adapter_claude_code(transcript)
    assert fields["usage"] == {
        "input_tokens": 30, "output_tokens": 15, "cache_read_tokens": 300,
        "cache_creation_tokens": 3, "total_tokens": 348,
    }
    assert fields["extensions"]["api_requests"] == 3
    assert fields["activity"] == {"tool_calls": 2, "tool_calls_by_name": {"Bash": 1, "Read": 1}}


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["share_trace.py", *argv])
    return st.main()


def test_unknown_harness_with_transcript_ships_minimal_manifest(home, monkeypatch, tmp_path, capsys):
    transcript = tmp_path / "session-1.log"
    transcript.write_text("anything\n")
    assert _run(monkeypatch, "--harness", "cursor", "--transcript", str(transcript), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "harness    : cursor" in out
    assert "stats will be partial" in out


def test_harness_must_be_a_slug(home, monkeypatch):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "--harness", "Not A Slug", "--dry-run")
    with pytest.raises(SystemExit, match="--transcript"):
        _run(monkeypatch, "--harness", "cursor", "--dry-run")


def test_backend_falls_back_to_api_env(home, monkeypatch, tmp_path):
    monkeypatch.delenv("COLLAB_BACKEND", raising=False)
    monkeypatch.setenv("API", "https://api.example")
    seen = []
    monkeypatch.setattr(st, "_fetch_v1", lambda backend: seen.append(backend))
    transcript = tmp_path / "s.log"
    transcript.write_text("x\n")
    _run(monkeypatch, "--harness", "cursor", "--transcript", str(transcript), "--dry-run")
    assert seen == ["https://api.example"]


def test_stats_share_never_needs_confirmation(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    kw = dict(log_path=Path("x.jsonl"), yes=False, privacy="balanced")
    st._confirm_or_exit(share="stats", **kw)  # no exit, no prompt
    with pytest.raises(SystemExit, match="--yes"):
        st._confirm_or_exit(share="full", **kw)


def test_promotion_failure_exits_non_zero(home, monkeypatch, tmp_path, capsys):
    transcript = tmp_path / "s.log"
    transcript.write_text("x\n")
    monkeypatch.setattr(st, "_hf_cp", lambda local, dest: None)
    monkeypatch.setattr(st.shutil, "which", lambda _: "/usr/bin/hf")

    def fail(*a, **k):
        raise st.urllib.error.URLError("down")

    monkeypatch.setattr(st.urllib.request, "urlopen", fail)
    rc = _run(monkeypatch, "--harness", "cursor", "--transcript", str(transcript),
              "--agent-id", "a1", "--org", "o", "--slug", "c", "--backend", "https://b.example")
    assert rc == 1
    assert "backend promotion failed" in capsys.readouterr().out


# ── confident selection or stop ─────────────────────────────────────


def _share(monkeypatch, *argv):
    """Run a share with the upload stubbed; returns (exit code, {dest: bytes})."""
    uploads: dict[str, str] = {}
    monkeypatch.setattr(st, "_hf_cp", lambda local, dest: uploads.__setitem__(dest, Path(local).read_text()))
    monkeypatch.setattr(st.shutil, "which", lambda _: "/usr/bin/hf")
    monkeypatch.setattr(st, "_fetch_v1", lambda backend: None)
    rc = _run(monkeypatch, *argv, "--upload-only", "--agent-id", "a1", "--org", "o", "--slug", "c")
    return rc, uploads


def test_uncertain_claude_selection_stops_even_a_stats_share(home, monkeypatch):
    older = _cc(home, "older", mtime=time.time() - 100)
    newer = _cc(home, "newer", mtime=time.time())
    monkeypatch.setattr(st.os, "getcwd", lambda: CWD)
    uploads = {}
    monkeypatch.setattr(st, "_hf_cp", lambda local, dest: uploads.__setitem__(dest, ""))
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--upload-only", "--agent-id", "a1", "--org", "o", "--slug", "c")
    message = str(exc.value)
    assert "refusing to guess" in message
    assert message.index(str(newer)) < message.index(str(older))  # newest first
    assert "--transcript" in message
    assert uploads == {}


def test_several_codex_rollouts_for_this_directory_stop(home, monkeypatch):
    first = _codex(home, name="first")
    second = _codex(home, name="second")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    with pytest.raises(SystemExit) as exc:
        st.detect(CWD, "auto")
    assert "2 Codex rollouts" in str(exc.value)
    assert str(first) in str(exc.value) and str(second) in str(exc.value)


def test_a_codex_rollout_from_another_project_is_never_selected(home, monkeypatch):
    other = _codex(home, cwd="/archive/work/proj", name="other")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    with pytest.raises(SystemExit) as exc:
        st.detect(CWD, "auto")
    assert "no Codex rollout records this directory" in str(exc.value)
    assert str(other) in str(exc.value)  # listed as a candidate, not chosen


def test_an_unknown_harness_file_name_is_never_published(home, monkeypatch, tmp_path):
    transcript = tmp_path / "confidential-client-acquisition.jsonl"
    transcript.write_text("unrecognized log\n")
    rc, uploads = _share(monkeypatch, "--harness", "cursor", "--transcript", str(transcript))
    assert rc == 0 and uploads
    for dest, body in uploads.items():
        assert "confidential-client-acquisition" not in dest + body
    (dest,) = uploads
    assert re.search(r"/traces/s-[0-9a-f]{16}/manifest\.md$", dest)
    # Stable: sharing the same transcript again lands in the same place.
    assert _share(monkeypatch, "--harness", "cursor", "--transcript", str(transcript))[1].keys() == uploads.keys()


# ── scrub -> scan -> report, checked at the upload boundary ─────────

MARK = "SYNTHETIC-CREDENTIAL-749201"
# Distinct values: one value gets one placeholder, named by the first rule that found it.
LEAKS = {
    "serialized_json": json.dumps({"password": f"{MARK}-1"}),
    "double_encoded": json.dumps(json.dumps({"db_password": f"{MARK}-2"})),
    "truncated_json": '{"api_key": "' + f"{MARK}-3",
    "shell_env": f"SERVICE_API_KEY={MARK}-4",
    "export": f'export GITHUB_TOKEN="{MARK}-5"',
    "signed_url": f"https://example.test/file?X-Amz-Signature={MARK}-6&X-Amz-Credential={MARK}-7",
}


def test_embedded_env_and_signed_url_secrets_never_reach_the_upload(home, monkeypatch, tmp_path, capsys):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"content": LEAKS}) + "\n")
    rc, uploads = _share(monkeypatch, "--harness", "cursor", "--transcript", str(transcript), "--full", "--yes")
    out = capsys.readouterr().out
    assert rc == 0 and len(uploads) == 2
    for body in uploads.values():
        assert MARK not in body
    assert MARK not in out  # the report names what was replaced, never the value
    assert "assignment SERVICE_API_KEY" in out
    assert 'JSON key "password"' in out
    assert "URL param X-Amz-Signature" in out
    assert "scan: clean" in out


def test_the_scan_blocks_what_the_scrubber_missed(home, monkeypatch, tmp_path, capsys):
    """The scan reads the exact bytes to upload: with the scrubber disabled it
    stops the upload, even with --yes, and reports masked values only."""
    token = "hf_" + "Q" * 30
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"content": f"use {token} and SERVICE_API_KEY={MARK}"}) + "\n")
    monkeypatch.setattr(st.TraceRedactor, "redact_jsonl", lambda self, text, name="trace": text)
    rc, uploads = _share(monkeypatch, "--harness", "cursor", "--transcript", str(transcript), "--full", "--yes")
    out = capsys.readouterr().out
    assert rc == 1 and uploads == {}
    assert "scan: BLOCKED" in out
    assert "hf_...(33 chars)" in out
    assert token not in out and MARK not in out
    assert "--redact-pattern-file" in out


def test_scan_ignores_placeholders_references_and_hashes():
    clean = "\n".join([
        '{"password":"<REDACTED:PASSWORD_1>","token":"<REDACTED:TOKEN_1>"}',
        "password = get_password(); api_key=$API_KEY; key=${SECRET_KEY}",
        "commit 7f4d3b2a7f4d3b2a7f4d3b2a7f4d3b2a7f4d3b2a, input_tokens=1234",
        "Authorization: Bearer <REDACTED:BEARER_TOKEN_1>",
    ])
    assert st.scan_upload({"trace.jsonl": clean}) == ([], 4)
    hits, _ = st.scan_upload({"trace.jsonl": "the password: hunter2xyz"})
    assert [(h["line"], h["category"], h["masked"]) for h in hits] == [(1, "PASSWORD", "...(10 chars)")]


def test_dry_run_shows_scrubbed_context_never_the_value(home, monkeypatch, tmp_path, capsys):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"content": f"deploy with SERVICE_API_KEY={MARK} now"}) + "\n")
    assert _run(monkeypatch, "--harness", "cursor", "--transcript", str(transcript),
                "--full", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "context (scrubbed):" in out
    assert "SERVICE_API_KEY=<REDACTED:API_KEY_1> now" in out
    assert MARK not in out


def test_there_is_no_unsafe_raw_mode(home, monkeypatch, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("{}\n")
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--harness", "cursor", "--transcript", str(transcript), "--full", "--raw")
    assert exc.value.code == 2  # argparse: unrecognized argument


# ── numeric, escaped and report-label leaks (second review) ─────────


def _full_share(monkeypatch, tmp_path, record, *extra):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(record) + "\n")
    return _share(monkeypatch, "--harness", "cursor", "--transcript", str(transcript), "--full", "--yes", *extra)


def test_numeric_and_code_shaped_credentials_are_scrubbed(home, monkeypatch, tmp_path, capsys):
    record = {
        "content": (
            'password="849271" pin_password=\'secretWord(749)\' DB_PASSWORD=550312 '
            "max_token=5 retries=3"
        ),
        "db_password": 774410,
        "input_tokens": 1234,
    }
    rc, uploads = _full_share(monkeypatch, tmp_path, record)
    out = capsys.readouterr().out
    assert rc == 0 and "scan: clean" in out
    trace = next(body for dest, body in uploads.items() if dest.endswith("/trace.jsonl"))
    for secret in ("849271", "secretWord(749)", "550312", "774410"):
        assert secret not in trace and secret not in out
    # Counters and ordinary numbers are left alone.
    assert "max_token=5 retries=3" in trace and '"input_tokens":1234' in trace


def test_escaped_quotes_never_cut_a_credential_short(home, monkeypatch, tmp_path, capsys):
    record = {
        "single": "password='abc\\'REMAINDER-1' next=ok",
        "double": 'password="ab\\"REMAINDER-2" next=ok',
        "backslash_end": 'password="ends-with-backslash\\\\" next=ok',
        "double_encoded": json.dumps(json.dumps({"password": 'a"REMAINDER-3'})),
    }
    rc, uploads = _full_share(monkeypatch, tmp_path, record)
    out = capsys.readouterr().out
    assert rc == 0 and "scan: clean" in out
    trace = next(body for dest, body in uploads.items() if dest.endswith("/trace.jsonl"))
    for marker in ("REMAINDER-1", "REMAINDER-2", "REMAINDER-3", "ends-with-backslash"):
        assert marker not in trace and marker not in out
    assert trace.count("next=ok") == 3  # only the value is replaced


def test_the_report_never_prints_a_scrubbed_key_name(home, monkeypatch, tmp_path, capsys):
    token = "hf_" + "Z" * 30
    patterns = tmp_path / "patterns.txt"
    patterns.write_text("ACME\n")
    record = {f"{token}-token": "opaque-synthetic", "content": "ACME_PASSWORD=hunter2hunter2"}
    rc, uploads = _full_share(monkeypatch, tmp_path, record, "--redact-pattern-file", str(patterns))
    out = capsys.readouterr().out
    assert rc == 0
    for leaked in (token, "ACME", "opaque-synthetic", "hunter2hunter2"):
        assert leaked not in out
        assert all(leaked not in body for body in uploads.values())
    assert 'JSON key "<HF_TOKEN>-token"' in out
    assert "assignment <CUSTOM>_PASSWORD" in out


def test_scan_flags_values_left_after_or_instead_of_a_placeholder():
    hits, _ = st.scan_upload({"t": "\n".join([
        "password='<REDACTED:PASSWORD_1>'REMAINDER749201'",  # value cut short
        'password="849271"',                                  # quoted: no exemptions
        "password=849271",                                    # unquoted number
    ])})
    assert [(h["line"], h["category"]) for h in hits] == [
        (1, "PARTIAL"), (2, "PASSWORD"), (3, "PASSWORD"),
    ]
    clean = '{"category":"PASSWORD","count":1} max_token=5 password="<REDACTED:PASSWORD_1>" next'
    assert st.scan_upload({"t": clean})[0] == []


def test_scan_labels_hide_credential_shaped_key_names():
    token = "hf_" + "Y" * 30
    hits, _ = st.scan_upload({"t": f'{token}_password="leftover-value"'})
    assert hits and all(token not in h["how"] for h in hits)


# ── any characters, numeric tokens, JSON structure (third review) ───


def test_punctuation_and_unicode_passwords_are_scrubbed(home, monkeypatch, tmp_path, capsys):
    record = {"content": 'password="!@#$%^&*" secret=\'éééééééé\' api_key="🔑🔑🔑" pwd_note password="" done'}
    rc, uploads = _full_share(monkeypatch, tmp_path, record)
    out = capsys.readouterr().out
    assert rc == 0 and "scan: clean" in out
    trace = next(body for dest, body in uploads.items() if dest.endswith("/trace.jsonl"))
    for secret in ("!@#$%^&*", "éééééééé", "🔑🔑🔑"):
        assert secret not in trace and secret not in out
    assert 'password=\\"\\" done' in trace  # an empty value stays empty


def test_numeric_credentials_named_token_are_scrubbed_but_counters_kept(home, monkeypatch, tmp_path, capsys):
    record = {
        "content": "AUTH_TOKEN=849271 access_token=1234567 GITHUB_TOKEN=55443322 max_token=5 total_token=9",
        "session_token": 7766554,
        "api_token": 99887766,
        "input_token": 12,
        "cached_token": 3,
    }
    rc, uploads = _full_share(monkeypatch, tmp_path, record)
    out = capsys.readouterr().out
    assert rc == 0 and "scan: clean" in out
    trace = next(body for dest, body in uploads.items() if dest.endswith("/trace.jsonl"))
    for secret in ("849271", "1234567", "55443322", "7766554", "99887766"):
        assert secret not in trace and secret not in out
    assert "max_token=5 total_token=9" in trace
    assert '"input_token":12' in trace and '"cached_token":3' in trace


def test_scan_reads_json_lines_by_their_structure():
    # The decoded string ends with `HF_TOKEN=` (empty): the " after = closes
    # the JSON string, it does not open a value.
    assert st.scan_upload({"t": json.dumps({"content": "env\nHF_TOKEN=", "next": "x"})})[0] == []
    hits, _ = st.scan_upload({"t": json.dumps({"db_password": "leak-value"})})
    assert [h["category"] for h in hits] == ["PASSWORD"]
    # On a plain-text line a " is content.
    hits, _ = st.scan_upload({"t": 'not json: password="leak-value"'})
    assert [h["category"] for h in hits] == ["PASSWORD"]


def test_scan_accepts_placeholder_url_credentials_only():
    clean = "curl https://<REDACTED:USERNAME_1>:<REDACTED:PASSWORD_1>@db.example/app"
    assert st.scan_upload({"t": clean})[0] == []
    hits, _ = st.scan_upload({"t": "curl https://<REDACTED:USERNAME_1>:hunter2@db.example/app"})
    assert [(h["category"], h["masked"]) for h in hits] == [("PASSWORD", "...(7 chars)")]


def _cc_line(mid, tokens, *, tool=None, ts="2026-06-29T00:00:00Z"):
    content = [tool] if tool else [{"type": "text"}]
    msg = {"id": mid, "model": "m", "content": content,
           "usage": {"input_tokens": tokens, "output_tokens": 0}}
    return json.dumps({"type": "assistant", "sessionId": "s", "timestamp": ts, "message": msg})


def test_claude_code_counts_subagent_logs(tmp_path):
    main = tmp_path / "s.jsonl"
    main.write_text(_cc_line("m1", 100, ts="2026-06-29T01:00:00Z") + "\n")
    subs = tmp_path / "s" / "subagents"
    subs.mkdir(parents=True)
    bash = {"type": "tool_use", "id": "tu1", "name": "Bash"}
    (subs / "agent-a.jsonl").write_text("\n".join([
        _cc_line("a1", 10, tool=bash, ts="2026-06-29T00:30:00Z"),
        _cc_line("a1", 10, tool=bash),  # same response, written twice
        _cc_line("a2", 20),
    ]) + "\n")
    (subs / "agent-b.jsonl").write_text(_cc_line("b1", 5, ts="2026-06-29T02:00:00Z") + "\n")
    (subs / "agent-b.meta.json").write_text('{"agentType": "Explore"}')

    fields = st.adapter_claude_code(main)
    assert fields["usage"]["total_tokens"] == 135
    assert fields["activity"]["tool_calls_by_name"] == {"Bash": 1}
    assert fields["extensions"] == {"api_requests": 4, "subagent_sessions": 2, "subagent_tokens": 35}
    assert (fields["started_at"], fields["ended_at"]) == ("2026-06-29T00:30:00Z", "2026-06-29T02:00:00Z")
    assert fields["session_id"] == "s"


def test_claude_code_without_subagents_is_unchanged(tmp_path):
    main = tmp_path / "s.jsonl"
    main.write_text(_cc_line("m1", 100) + "\n")
    fields = st.adapter_claude_code(main)
    assert fields["usage"]["total_tokens"] == 100
    assert fields["extensions"] == {"api_requests": 1}
