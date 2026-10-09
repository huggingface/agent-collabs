#!/usr/bin/env python3
"""share_trace.py — share this session's stats (and optionally its full trace).

The deliberate, session-boundary command (see TRACES_DESIGN.md). It parses your
harness's NATIVE session log into a small manifest (token + tool-call stats),
writes the bundle into YOUR OWN scratch bucket, and calls ``POST /v1/traces`` —
the same promote ergonomic as results/artifacts. Identity is your bucket name;
no token rides on the call.

WHAT THIS READS AND UPLOADS — for operators, agents, and safety classifiers:
  * Reads ONLY this session's harness log (auto-detected from the invoking
    harness's env, or the explicit --transcript), plus an explicitly supplied
    --redact-pattern-file. It does NOT read .env, credential files, or anything
    else in the project.
  * The DEFAULT (`stats`) uploads a small manifest: harness, session id, model,
    start/end times, token counts, tool-call counts by tool name, and redaction
    counts — no prompts, no code, no file contents. `--full` additionally
    uploads the native session log, scrubbed CLIENT-SIDE first: credentials and
    personal identifiers are replaced with typed placeholders.
  * Before anything is written, a separate scan reads the exact bytes about to
    be uploaded for credentials the scrubber missed. Any hit (or a scan that
    cannot finish) blocks the upload; `--yes` never overrides it.
  * Every run prints a short report: what was replaced (by kind, key or pattern
    and line — never the value) and the scan result. Read it for anything the
    scrubber should have caught but didn't; reading the whole transcript is
    not required. `--dry-run` adds the scrubbed context around each replacement.
  * Destination is YOUR OWN Hugging Face scratch bucket in the challenge org (via
    the `hf` CLI you already use), then the collab's own API — never an arbitrary
    external host. It uses your existing `hf` login; the script reads/transmits
    no token, and manifest strings pass through the same scrubber.

    python share_trace.py                 # stats only; no content leaves (the floor)
    python share_trace.py --upload-only   # write to scratch bucket; skip backend promotion
    python share_trace.py --full --yes    # FULL: stats + balanced-scrubbed log -> library
    python share_trace.py --full --privacy secrets  # credentials only; keep emails/home paths
    python share_trace.py --full --privacy strict   # also pseudonymize hosts + IPs
    python share_trace.py --full --redact-pattern-file patterns.txt  # + your own regexes
    python share_trace.py --dry-run       # print the plan, report and manifest; touch nothing

`full` lets Hugging Face's built-in trace viewer render the native log directly
from the bucket (Claude Code & Codex supported out of the box). Scrubbing parses
JSONL and makes surgical, typed substitutions that preserve prompts, responses,
commands, tool structure, and relative paths. It is best-effort: no heuristic
can tell whether ordinary task prose or source code is confidential. Your
scratch bucket is already readable by everyone in the org, so content is
scrubbed before it is written there at all. If the session holds confidential
material, add task-specific regexes with --redact-pattern-file (one per line)
or share stats only. `--full` needs confirmation; pass `--yes` for
non-interactive / agent runs.

Auto-detection follows the harness that INVOKES this script (from its env —
Claude Code's CLAUDE_CODE_SESSION_ID pins the *exact* session), so multiple
agents sharing one directory are never cross-attributed. It shares only a
session it is sure of: the pinned Claude Code session, the only Claude Code
log for this directory, or the only Codex rollout recorded in it. Otherwise it
stops and prints `--transcript` commands for the candidates. Override with
`--harness` / `--transcript`.

SELF-CONTAINED + DEPENDENCY-FREE by design: download this one file and run it
under ANY `python3` — no `pip install`. The frontmatter is emitted as JSON
(which is valid YAML, so the backend parses it identically) and the upload
shells out to the `hf` CLI (which you already use for `hf auth login`). Org/slug
are auto-discovered from the backend's `GET /v1`, so you only need `--backend`
(or `COLLAB_BACKEND`, else `API`) and your `--agent-id`. The per-harness
adapters are inlined below; keep them in sync with the verified recipes (memory:
cc-codex-trace-metric-extraction).
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn


ADAPTER_VERSION = 2  # v2: Claude Code usage deduped per message.id (v1 over-counted)
REDACTOR_VERSION = 3  # v3: embedded JSON, env-style names, signed URLs
# Keep in sync with KNOWN_FULL_HARNESSES in backend/app/trace_stats.py.
KNOWN_HARNESSES = ("claude-code", "codex")
PRIVACY_LEVELS = ("secrets", "balanced", "strict")


# ════════════════════════ per-harness adapters ════════════════════════
# A harness's NATIVE local session log -> manifest fields. NOT OpenTelemetry.
# Cardinal rule: a metric we couldn't determine is OMITTED (null = unknown);
# only a measured zero is 0. Parse defensively — these formats are unversioned.

def _jsonl(path: Path):
    """Yield parsed JSON objects from a .jsonl file, skipping unparseable lines."""
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _int(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def adapter_claude_code(log_path: Path) -> dict:
    """$CLAUDE_CONFIG_DIR (default ~/.claude)/projects/<slug>/<session_id>.jsonl.

    Claude Code writes one `assistant` line per content block, repeating the
    API response's message.id and usage — so usage is kept per message.id
    (last write wins) and summed once; tool calls are deduped by tool_use id."""
    responses: dict = {}  # message.id (or a per-line key if absent) -> usage
    tools: dict[str, int] = {}
    seen_tools: set = set()
    model = None
    session_id = None  # from the records; never the file name
    first_ts = last_ts = None

    for rec in _jsonl(log_path):
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if rec.get("sessionId"):
            session_id = rec["sessionId"]
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        if msg.get("model"):
            model = msg["model"]
        key = msg.get("id") or ("line", len(responses))
        responses[key] = msg.get("usage") or responses.get(key) or {}
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                if block.get("id"):
                    if block["id"] in seen_tools:
                        continue
                    seen_tools.add(block["id"])
                name = block.get("name") or "?"
                tools[name] = tools.get(name, 0) + 1

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    saw_usage = False
    for u in responses.values():
        if u:
            saw_usage = True
            usage["input_tokens"] += _int(u.get("input_tokens")) or 0
            usage["output_tokens"] += _int(u.get("output_tokens")) or 0
            usage["cache_read_tokens"] += _int(u.get("cache_read_input_tokens")) or 0
            usage["cache_creation_tokens"] += _int(u.get("cache_creation_input_tokens")) or 0

    fields: dict = {
        "harness": "claude-code",
        "session_id": session_id,
        "model": model,
        "started_at": first_ts,
        "ended_at": last_ts,
        "activity": {"tool_calls": sum(tools.values()), "tool_calls_by_name": tools},
        "extensions": {"api_requests": len(responses)},
    }
    if saw_usage:
        usage["total_tokens"] = sum(usage.values())
        fields["usage"] = usage
    return fields


_CODEX_TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")


def adapter_codex(log_path: Path) -> dict:
    """$CODEX_HOME (default ~/.codex)/sessions/YYYY/MM/DD/rollout-*.jsonl — token_count is CUMULATIVE
    (take the last); dedupe tool calls by call_id (MCP appears twice)."""
    last_usage = None
    tools: dict[str, int] = {}
    seen_calls: set[str] = set()
    turns = 0
    model = None
    session_id = None
    first_ts = last_ts = None

    def _count(name: str, call_id) -> None:
        if call_id is not None:
            if call_id in seen_calls:
                return
            seen_calls.add(call_id)
        tools[name] = tools.get(name, 0) + 1

    for rec in _jsonl(log_path):
        ts = rec.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        typ = rec.get("type")
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if typ == "session_meta":
            session_id = payload.get("session_id") or payload.get("id") or session_id
            model = model or payload.get("model")
        elif typ == "turn_context":
            model = model or payload.get("model")
        elif typ == "response_item":
            pt = payload.get("type")
            if pt in _CODEX_TOOL_TYPES:
                name = payload.get("name") or ("shell" if pt == "local_shell_call" else pt)
                _count(name, payload.get("call_id"))
        elif typ == "event_msg":
            pt = payload.get("type")
            if pt == "token_count":
                info = payload.get("info") or {}
                if info.get("total_token_usage"):
                    last_usage = info["total_token_usage"]
            elif pt in ("task_complete", "turn_complete"):
                turns += 1
            elif pt == "mcp_tool_call_end":
                _count(payload.get("tool") or payload.get("name") or "mcp", payload.get("call_id"))

    fields: dict = {
        "harness": "codex",
        "session_id": session_id,
        "model": model,
        "started_at": first_ts,
        "ended_at": last_ts,
        "activity": {"tool_calls": sum(tools.values()), "tool_calls_by_name": tools},
        "extensions": {"turns": turns},
    }
    if last_usage:
        fields["usage"] = {
            "input_tokens": _int(last_usage.get("input_tokens")),
            "output_tokens": _int(last_usage.get("output_tokens")),
            "cache_read_tokens": _int(last_usage.get("cached_input_tokens")),
            "cache_creation_tokens": None,  # Codex doesn't separate cache-creation
            "reasoning_tokens": _int(last_usage.get("reasoning_output_tokens")),
            "total_tokens": _int(last_usage.get("total_tokens")),
        }
    return fields


def adapter_minimal(log_path: Path, harness: str) -> dict:
    """Unknown harness: ship the raw log + a minimal manifest. No stats — the
    backend records this as `partial` and never blocks participation."""
    return {
        "harness": harness,
        "session_id": None,  # no adapter: never derive it from the file name
        "model": None,
        "started_at": None,
        "ended_at": None,
    }


ADAPTERS = {"claude-code": adapter_claude_code, "codex": adapter_codex}


def build_fields(harness: str, log_path: Path) -> dict:
    fn = ADAPTERS.get(harness)
    return fn(log_path) if fn else adapter_minimal(log_path, harness)


def _claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _cc_project_dir(cwd: str) -> Path:
    slug = re.sub(r"[/._]", "-", os.path.abspath(cwd))
    return _claude_dir() / "projects" / slug


def _codex_logs() -> list[Path]:
    codex_root = _codex_home() / "sessions"
    return sorted(
        [Path(p) for p in glob.glob(str(codex_root / "**" / "rollout-*.jsonl"), recursive=True)],
        key=lambda p: p.stat().st_mtime if p.is_file() else 0,
        reverse=True,
    )


def _codex_session_cwd(path: Path) -> str | None:
    """The working directory a Codex rollout records in its session_meta: the
    authoritative value. A path merely mentioned in the log (a command, a
    file) says nothing about where the session ran."""
    for i, rec in enumerate(_jsonl(path)):
        if i >= 5:  # session_meta is the first record
            break
        if isinstance(rec, dict) and rec.get("type") == "session_meta":
            cwd = (rec.get("payload") or {}).get("cwd")
            return cwd if isinstance(cwd, str) else None
    return None


def _same_dir(a: str, b: str) -> bool:
    return os.path.realpath(os.path.expanduser(a)) == os.path.realpath(os.path.expanduser(b))


def _codex_matches_cwd(path: Path, cwd: str) -> bool:
    recorded = _codex_session_cwd(path)
    return recorded is not None and _same_dir(recorded, cwd)


def _codex_for_cwd(cwd: str) -> list[Path]:
    """Codex rollouts recorded in exactly this directory, newest first."""
    return [p for p in _codex_logs() if _codex_matches_cwd(p, cwd)]


def _cc_logs(cwd: str) -> list[Path]:
    """Claude Code transcripts for this directory, newest first."""
    d = _cc_project_dir(cwd)
    files = [p for p in d.glob("*.jsonl") if p.is_file()] if d.is_dir() else []
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _refuse_uncertain(why: str, candidates: list[Path]) -> NoReturn:
    """Stop rather than guess: sharing the wrong session would publish another
    session's metadata (and, with --full, its content)."""
    lines = [f"{why}; refusing to guess which session to share."]
    if candidates:
        lines.append("Re-run with the same options plus --transcript for yours (newest first):")
        lines += [f"  --transcript {shlex.quote(str(p))}" for p in candidates[:5]]
        if len(candidates) > 5:
            lines.append(f"  ... and {len(candidates) - 5} more")
    else:
        lines.append("Re-run with --transcript <path to your session log>.")
    raise SystemExit("\n".join(lines))


def _infer_harness(log_path: Path) -> str | None:
    s = str(log_path.expanduser().resolve())
    codex_root = str(_codex_home().resolve() / "sessions") + os.sep
    claude_root = str(_claude_dir().resolve() / "projects") + os.sep
    if "/.codex/sessions/" in s or s.startswith(codex_root) or log_path.name.startswith("rollout-"):
        return "codex"
    if "/.claude/projects/" in s or s.startswith(claude_root):
        return "claude-code"
    return None


def _running_harness() -> tuple[str | None, str | None]:
    """Identify the harness INVOKING this script from its injected env, plus the
    exact session id when the harness exposes one. (None, None) if unknown.

    This is what keeps multiple agents in one directory from being cross-
    attributed (e.g. a Codex agent uploading a co-located Claude Code log):
    - Claude Code sets CLAUDE_CODE_SESSION_ID (the exact session) + CLAUDECODE=1.
    - Codex sets CODEX_SANDBOX* in its (default) sandboxed exec but exposes NO
      session id — so we know it's Codex, but still locate the rollout by cwd.
      (CODEX_HOME only relocates the logs; it is often exported globally, so
      it is NOT a marker.)
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if sid or os.environ.get("CLAUDECODE"):
        return "claude-code", (sid or None)
    if os.environ.get("CODEX_SANDBOX") or os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"):
        return "codex", None
    return None, None


def _cc_session_log(cwd: str, session_id: str) -> Path | None:
    """The exact Claude Code transcript for a session id, if it exists."""
    p = _cc_project_dir(cwd) / f"{session_id}.jsonl"
    return p if p.is_file() else None


def _detect_claude_code(cwd: str, session_id: str | None) -> Path:
    if session_id:
        pinned = _cc_session_log(cwd, session_id)
        if pinned:
            return pinned  # the exact invoking session
        raise SystemExit(
            f"CLAUDE_CODE_SESSION_ID={session_id} has no transcript under "
            f"{_cc_project_dir(cwd)}; refusing to guess another session. "
            "Pass --transcript <path> if the transcript lives elsewhere."
        )
    logs = _cc_logs(cwd)
    if len(logs) == 1:
        return logs[0]
    if not logs:
        raise SystemExit("could not find a Claude Code session for this directory; pass --transcript")
    _refuse_uncertain(
        f"{len(logs)} Claude Code sessions ran in this directory and the environment "
        "doesn't say which one is running",
        logs,
    )


def _detect_codex(cwd: str) -> Path:
    matches = _codex_for_cwd(cwd)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        _refuse_uncertain("no Codex rollout records this directory as its working directory",
                          _codex_logs())
    _refuse_uncertain(
        f"{len(matches)} Codex rollouts ran in this directory (Codex exposes no session id)",
        matches,
    )


def detect(cwd: str, harness: str) -> tuple[str, Path]:
    """Find the native session log of the agent INVOKING this script, or stop.

    Anchored on the invoking harness's environment (see _running_harness) so
    agents sharing a directory aren't cross-attributed. Only a confident
    selection is returned: the exact Claude Code session, the only Claude Code
    log for this directory, or the only Codex rollout recorded in it. Anything
    else stops with --transcript commands for the candidates: even a stats
    share publishes the session's id, model, timestamps and tool names.
    """
    env_harness, env_sid = _running_harness()

    if harness == "auto":
        if env_harness:
            harness = env_harness
        else:
            # The env doesn't say who's running. Use the sole harness with a
            # session here; if both have one, refuse rather than guess.
            cc = _cc_logs(cwd)
            cx = _codex_for_cwd(cwd)
            if cc and cx:
                raise SystemExit(
                    "multiple harnesses have a session for this directory and the "
                    "environment doesn't identify the running agent — pass "
                    "--harness claude-code|codex (or --transcript <path>)."
                )
            if cc:
                harness = "claude-code"
            elif cx:
                harness = "codex"
            else:
                raise SystemExit(
                    "could not auto-detect a session log; pass --harness and --transcript"
                )
    elif env_harness and env_harness != harness:
        print(f"warning: --harness {harness}, but this looks like a {env_harness} "
              "session from the environment; proceeding as requested.")

    if harness == "claude-code":
        return "claude-code", _detect_claude_code(cwd, env_sid)
    if harness == "codex":
        return "codex", _detect_codex(cwd)
    raise SystemExit(f"unknown harness: {harness!r}")


# ════════════════════════ manifest + upload ════════════════════════

# Best-effort, structure-preserving scrubber. Provider signatures catch secrets
# wherever they appear; context patterns catch opaque values next to sensitive
# names. Deliberately avoid generic entropy detection: traces legitimately
# contain commit SHAs, call IDs, hashes, and generated identifiers.
#
# (category, how the report names it, pattern). Anthropic keys come before the
# OpenAI-style rule because both start with "sk-".
_PROVIDER_SECRET_PATTERNS = [
    ("HF_TOKEN", "hf_ token",
     re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])")),
    ("GITHUB_TOKEN", "GitHub token",
     re.compile(
         r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|"
         r"github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_])"
     )),
    ("ANTHROPIC_KEY", "sk-ant- key (Anthropic)",
     re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("OPENAI_KEY", "sk- key (OpenAI-style)",
     re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("AWS_ACCESS_KEY", "AWS access key id",
     re.compile(r"(?<![0-9A-Z])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])")),
    ("SLACK_TOKEN", "Slack token",
     re.compile(r"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])")),
    ("GITLAB_TOKEN", "GitLab token",
     re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("GOOGLE_API_KEY", "AIza key (Google)",
     re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])")),
    ("NPM_TOKEN", "npm token",
     re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}(?![A-Za-z0-9_])")),
    ("PYPI_TOKEN", "PyPI token",
     re.compile(r"(?<![A-Za-z0-9_-])pypi-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("JWT", "JWT",
     re.compile(
         r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
         r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
     )),
]

# Each provider token starts with a fixed string; checking for it with `in`
# first skips the regex on almost every string.
_PROVIDER_NEEDLES = {
    "HF_TOKEN": ("hf_",), "GITHUB_TOKEN": ("gh", "github_pat_"), "ANTHROPIC_KEY": ("sk-ant-",),
    "OPENAI_KEY": ("sk-",), "AWS_ACCESS_KEY": ("AKIA", "ASIA"), "SLACK_TOKEN": ("xox",),
    "GITLAB_TOKEN": ("glpat-",), "GOOGLE_API_KEY": ("AIza",), "NPM_TOKEN": ("npm_",),
    "PYPI_TOKEN": ("pypi-",), "JWT": ("eyJ",),
}


def _providers_in(text: str):
    """The provider patterns whose fixed prefix occurs in `text`."""
    for category, how, pattern in _PROVIDER_SECRET_PATTERNS:
        if any(needle in text for needle in _PROVIDER_NEEDLES[category]):
            yield category, how, pattern


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?)-----.*?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:proxy[-_])?authorization\b"
    r"(?:\\?[\"']?\s*[:=]\s*\\?[\"']?\s*))"
    r"(?P<scheme>bearer|basic|token|api[-_]?key)\s+"
    r"(?P<value>[A-Za-z0-9._~+/\-=]{4,})"
)
_COOKIE_HEADER_RE = re.compile(
    r"(?i)(?P<prefix>\b(?:set-cookie|cookie)\b\s*:\s*)"
    r"(?P<value>[^\"'\\\r\n]+)"
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?i)(?P<scheme>\b(?:https?|postgres(?:ql)?|mysql|mariadb|"
    r"mongodb(?:\+srv)?|redis|amqps?|ssh|sftp|ftp)://)"
    r"(?P<username>[^:@/\s]+):(?P<password>[^@/\s]+)@"
)
# Query parameters that carry a credential, signed-URL signatures included.
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?P<key>access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"api[_-]?key|token|secret|password|sig|signature|"
    r"x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"x-goog-signature|x-goog-credential)=)"
    r"(?P<value>[^&#\s\"'\\]+)"
)
# Names that mark a secret value: `password=`, `"api_key": `, `DB_PASSWORD=`,
# `SERVICE_API_KEY=`, `GITHUB_TOKEN=`. A name may carry a prefix (`db_password`)
# but no suffix (`password_hint`, `secretary`); a bare `token` is too common,
# and so is `PWD=` (every environment dump has the working directory).
_SECRET_NAME_CORE = (
    r"password|passwd|secret|client[_-]?secret|secret[_-]?key|"
    r"api[_-]?key|apikey|private[_-]?key|access[_-]?key|"
    r"(?:access|refresh|id|session|auth|bearer)[_-]?token|credentials?"
)
# Matches the secret word itself; a prefix (`db_`, `SERVICE_`) is recovered by
# _full_name for the report. Matching the prefix in the regex made the engine
# retry at nearly every position of long lines.
_SECRET_NAME = (
    rf"(?<![A-Za-z0-9])(?:{_SECRET_NAME_CORE}|(?<=[_.-])token)(?![A-Za-z0-9])"
)
_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")


def _full_name(m: re.Match) -> str:
    """The whole name a secret word belongs to: `SERVICE_API_KEY` for the
    match `API_KEY`."""
    text, start = m.string, m.start("key")
    while start > 0 and text[start - 1] in _NAME_CHARS:
        start -= 1
    return text[start:m.end("key")]
# The start of `name = "value"` / `name: 'value'`, also as JSON text and with
# JSON-escaped quotes (`\"password\": \"...\"`). Where the value ends is found
# by _quoted_value_end, which honours backslash escapes. A quoted value is a
# literal, so it is always replaced, whatever it looks like.
_QUOTED_SECRET_START_RE = re.compile(
    rf"(?i)(?P<key>{_SECRET_NAME})(?P<kq>\\?[\"']?)(?P<sep>\s*[:=]\s*)(?P<quote>\\?[\"'])"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<key>{_SECRET_NAME})(?P<kq>\\?[\"']?)(?P<sep>\s*[:=]\s*)(?!\\?[\"'])"
    r"(?P<value>[^\s,;}\]\"'\\]+)"
)
# Unquoted values next to a secret name that are code, not a secret: null,
# booleans, masks, placeholders, and references such as $VAR, ${VAR},
# os.environ[...] or a function call. Numbers are secrets (a numeric password)
# except after a *_token name, where they are counters (max_token=5).
_UNQUOTED_REFERENCE_RE = re.compile(
    r"(?is)^(?:null|none|nil|true|false|undefined|\*+|x{3,}|"
    r"<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?.*|%[A-Za-z_]+%|\{\{.*\}\}|"
    r"(?:os\.environ|os\.getenv|process\.env|env)\b.*|[A-Za-z_][A-Za-z0-9_.]*\(.*)$"
)
_NUMBER_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")
# A credential has at least one letter or digit; a lone "," or "" does not.
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@"
    r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![A-Za-z0-9.-])"
)
_POSIX_HOME_RE = re.compile(r"(?<![A-Za-z0-9])(?:/(?:Users|home)/[^/\s\"']+|/root)(?=/)")
_WINDOWS_HOME_RE = re.compile(r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:\\Users\\[^\\\s\"']+)(?=\\)")
_URL_HOST_RE = re.compile(
    r"(?i)(?P<prefix>\b[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?)"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|localhost|"
    r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}|(?:\d{1,3}\.){3}\d{1,3})"
)
_IPV4_RE = re.compile(r"(?<![A-Za-z0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9.])")
_PLACEHOLDER_RE = re.compile(r"^<REDACTED:[A-Z0-9_]+_\d+>$")

_SENSITIVE_KEYS = {
    "authorization": "AUTHORIZATION",
    "proxy_authorization": "AUTHORIZATION",
    "password": "PASSWORD",
    "passwd": "PASSWORD",
    "pwd": "PASSWORD",
    "secret": "SECRET",
    "secret_key": "SECRET",
    "client_secret": "CLIENT_SECRET",
    "api_key": "API_KEY",
    "apikey": "API_KEY",
    "x_api_key": "API_KEY",
    "access_token": "ACCESS_TOKEN",
    "refresh_token": "REFRESH_TOKEN",
    "id_token": "ID_TOKEN",
    "session_token": "SESSION_TOKEN",
    "token": "TOKEN",
    "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "aws_session_token": "AWS_SESSION_TOKEN",
    "cookie": "COOKIE",
    "set_cookie": "COOKIE",
    "private_key": "PRIVATE_KEY",
}
# A JSON key ending in one of these holds a secret when its value is a string:
# db_password, stripe_secret, github_token, service_api_key. (Numeric fields
# such as input_tokens are not strings and do not end in "_token".)
_SENSITIVE_KEY_SUFFIXES = (
    ("_password", "PASSWORD"),
    ("_passwd", "PASSWORD"),
    ("_secret", "SECRET"),
    ("_token", "TOKEN"),
    ("_api_key", "API_KEY"),
    ("_apikey", "API_KEY"),
    ("_private_key", "PRIVATE_KEY"),
    ("_access_key", "ACCESS_KEY"),
    ("_credentials", "CREDENTIAL"),
    ("_credential", "CREDENTIAL"),
)
# Pseudonymized personal data: summarized as one report row per category.
_COLLAPSED_CATEGORIES = {"EMAIL": "email addresses", "HOST": "host names", "IP": "IP addresses"}


def _normalise_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _sensitive_key_category(key: object, value: object) -> str | None:
    norm = _normalise_key(key)
    if norm in _SENSITIVE_KEYS:
        return _SENSITIVE_KEYS[norm]
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    for suffix, category in _SENSITIVE_KEY_SUFFIXES:
        # A string always; a number too (a numeric PIN or password), except
        # for *_token, where numbers are counters.
        if norm.endswith(suffix) and (isinstance(value, str) or (number and suffix != "_token")):
            return category
    return None


def _unquoted_secret(key: str, value: str) -> bool:
    """Whether an unquoted value after a secret name may be the secret itself
    (see _UNQUOTED_REFERENCE_RE)."""
    v = value.strip()
    if not v or _UNQUOTED_REFERENCE_RE.match(v):
        return False
    if _NUMBER_RE.match(v):
        return not _normalise_key(key).endswith("token")
    return True


def _quoted_value_end(text: str, start: int, quote: str) -> tuple[int, int]:
    """(end of the value, end of its closing quote) for a value opened by
    `quote` just before `start`. A quote character closes it only when the
    backslashes before it leave it unescaped: an even count for a plain quote,
    1 mod 4 for a JSON-escaped one (\\" inside a JSON string), whose value also
    stops where that JSON string ends. With no closing quote on the line, the
    value runs to the end of the line."""
    q, escaped = quote[-1], len(quote) == 2
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "\r\n":
            return i, i
        if ch == q:
            n, j = 0, i
            while j > start and text[j - 1] == "\\":
                n, j = n + 1, j - 1
            if not escaped and n % 2 == 0:
                return i, i + 1
            if escaped and n % 4 == 1:
                return i - 1, i + 1
            if escaped and n % 2 == 0:
                return i, i  # the enclosing JSON string ends: a cut-off value
        i += 1
    return len(text), len(text)


_OPAQUE_RUN_RE = re.compile(r"[A-Za-z0-9]{16,}")


def _sanitize_label(text: str, custom_patterns=(), privacy: str = "balanced") -> str:
    """A key or variable name as the report may print it: credential shapes,
    custom-pattern matches and (balanced/strict) emails become their category,
    as in the upload, and any opaque run of 16+ letters/digits (which no real
    key name has) becomes its length. A report never shows more than the
    scrubbed upload, and less when the scan has found something."""
    for category, _how, pattern in _providers_in(text):
        text = pattern.sub(f"<{category}>", text)
    for _lineno, pattern in custom_patterns:
        text = pattern.sub("<CUSTOM>", text)
    if privacy in ("balanced", "strict"):
        text = _EMAIL_RE.sub("<EMAIL>", text)
    return _OPAQUE_RUN_RE.sub(lambda m: f"<{len(m.group(0))} chars>", text)


def _custom_patterns(path: str | None) -> list[tuple[int, re.Pattern]]:
    """Load one non-empty regex per line from an explicitly supplied file, as
    (line number, pattern) so the report can say which line matched."""
    if not path:
        return []
    pattern_path = Path(path).expanduser()
    if not pattern_path.is_file():
        raise SystemExit(f"no such redaction pattern file: {pattern_path}")
    out = []
    for lineno, raw in enumerate(pattern_path.read_text(encoding="utf-8").splitlines(), 1):
        pattern = raw.strip()
        if not pattern or pattern.startswith("#"):
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise SystemExit(
                f"invalid regex in {pattern_path}:{lineno}: {exc}"
            ) from exc
        if compiled.search(""):
            raise SystemExit(
                f"redaction regex in {pattern_path}:{lineno} matches empty text"
            )
        out.append((lineno, compiled))
    return out


class TraceRedactor:
    """JSON-aware scrubber with stable, typed aliases for one trace. It also
    records where each alias came from (what kind of value, which key or
    pattern, which lines) for the redaction report — never the value."""

    def __init__(
        self,
        privacy: str = "balanced",
        custom_patterns: list[tuple[int, re.Pattern]] | None = None,
    ):
        if privacy not in PRIVACY_LEVELS:
            raise ValueError(f"unknown privacy level: {privacy!r}")
        self.privacy = privacy
        self.custom_patterns = custom_patterns or []
        self._aliases: dict[str, tuple[str, str]] = {}
        self._next: dict[str, int] = {}
        self._counts: dict[str, int] = {}
        # placeholder -> {"category", "how", "count", "where": [...]}
        self.origins: dict[str, dict] = {}
        self.location = "manifest"  # where the text being scrubbed will land
        self.json_lines = 0
        self.text_lines = 0

    @staticmethod
    def _is_placeholder(value: object) -> bool:
        return isinstance(value, str) and bool(_PLACEHOLDER_RE.fullmatch(value))

    def _label(self, name: str) -> str:
        return _sanitize_label(name, self.custom_patterns, self.privacy)

    def _redact_quoted_assignments(self, text: str) -> str:
        out, pos = [], 0
        for m in _QUOTED_SECRET_START_RE.finditer(text):
            if m.start() < pos:
                continue  # inside a value already replaced
            start = m.end()
            end, _close = _quoted_value_end(text, start, m.group("quote"))
            value = text[start:end]
            out.append(text[pos:start])
            if _ALNUM_RE.search(value) and not self._is_placeholder(value):
                key = _full_name(m)
                out.append(self._alias(_assignment_category(key), value, f"assignment {self._label(key)}"))
            else:
                out.append(value)
            pos = end
        out.append(text[pos:])
        return "".join(out)

    def _note(self, key: str, category: str, how: str) -> None:
        origin = self.origins.setdefault(
            key, {"category": category, "how": how, "count": 0, "where": []}
        )
        origin["count"] += 1
        # The first few places only: the report shows three and "...".
        if self.location not in origin["where"] and len(origin["where"]) < 4:
            origin["where"].append(self.location)
        self._counts[category] = self._counts.get(category, 0) + 1

    def _alias(self, category: str, value: object, how: str) -> str:
        if self._is_placeholder(value):
            return str(value)
        if isinstance(value, str):
            identity = value
        else:
            identity = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
        existing = self._aliases.get(identity)
        if existing:
            actual_category, placeholder = existing
        else:
            actual_category = re.sub(r"[^A-Z0-9]+", "_", category.upper()).strip("_")
            index = self._next.get(actual_category, 0) + 1
            self._next[actual_category] = index
            placeholder = f"<REDACTED:{actual_category}_{index}>"
            self._aliases[identity] = (actual_category, placeholder)
        self._note(placeholder, actual_category, how)
        return placeholder

    def _auth_value(self, value: str, how: str, category: str = "AUTHORIZATION") -> str:
        if self._is_placeholder(value):
            return value
        match = re.fullmatch(
            r"(?is)\s*(bearer|basic|token|api[-_]?key)\s+(.+?)\s*", value
        )
        if not match:
            return self._alias(category, value, how)
        scheme, credential = match.groups()
        kind = {
            "bearer": "BEARER_TOKEN",
            "basic": "BASIC_CREDENTIAL",
            "token": "AUTH_TOKEN",
            "apikey": "API_KEY",
            "api-key": "API_KEY",
            "api_key": "API_KEY",
        }[scheme.lower()]
        return f"{scheme} {self._alias(kind, credential, how)}"

    def _redact_text(self, text: str) -> str:
        if not text:
            return text

        text = _PRIVATE_KEY_RE.sub(
            lambda m: self._alias("PRIVATE_KEY", m.group(0), "private key block"), text
        )

        def auth_repl(match: re.Match) -> str:
            scheme = match.group("scheme")
            value = match.group("value")
            return match.group("prefix") + self._auth_value(
                f"{scheme} {value}", f"Authorization header ({scheme.lower()})"
            )

        text = _AUTH_HEADER_RE.sub(auth_repl, text)
        text = _COOKIE_HEADER_RE.sub(
            lambda m: m.group("prefix")
            + self._alias("COOKIE", m.group("value").rstrip(), "Cookie header")
            + m.group("value")[len(m.group("value").rstrip()):],
            text,
        )
        text = _CREDENTIAL_URL_RE.sub(
            lambda m: (
                m.group("scheme")
                + self._alias("USERNAME", m.group("username"), "user in a URL")
                + ":"
                + self._alias("PASSWORD", m.group("password"), "password in a URL")
                + "@"
            ),
            text,
        )
        text = _QUERY_SECRET_RE.sub(
            lambda m: m.group("prefix")
            + self._alias(
                _normalise_key(m.group("key")), m.group("value"), f"URL param {m.group('key')}"
            ),
            text,
        )

        def unquoted_repl(match: re.Match) -> str:
            key, value = _full_name(match), match.group("value")
            if self._is_placeholder(value) or not _unquoted_secret(key, value):
                return match.group(0)
            return (
                match.group("key")
                + match.group("kq")
                + match.group("sep")
                + self._alias(_assignment_category(key), value, f"assignment {self._label(key)}")
            )

        text = self._redact_quoted_assignments(text)
        text = _UNQUOTED_SECRET_RE.sub(unquoted_repl, text)
        for category, how, pattern in _providers_in(text):
            text = pattern.sub(lambda m, c=category, h=how: self._alias(c, m.group(0), h), text)
        for lineno, pattern in self.custom_patterns:
            text = pattern.sub(
                lambda m, n=lineno: self._alias("CUSTOM", m.group(0), f"pattern file, line {n}"),
                text,
            )

        if self.privacy in ("balanced", "strict"):
            text = _POSIX_HOME_RE.sub(
                lambda m: self._count_static("HOME_PATH", "$HOME", "home directory -> $HOME"), text
            )
            text = _WINDOWS_HOME_RE.sub(
                lambda m: self._count_static("HOME_PATH", "$HOME", "home directory -> $HOME"), text
            )
            text = _EMAIL_RE.sub(lambda m: self._alias("EMAIL", m.group(0), "email address"), text)

        if self.privacy == "strict":
            text = _URL_HOST_RE.sub(
                lambda m: m.group("prefix") + self._alias("HOST", m.group("host"), "host name"),
                text,
            )

            def ipv4_repl(match: re.Match) -> str:
                value = match.group(0)
                if any(int(part) > 255 for part in value.split(".")):
                    return value
                return self._alias("IP", value, "IP address")

            text = _IPV4_RE.sub(ipv4_repl, text)
        return text

    def _count_static(self, category: str, replacement: str, how: str) -> str:
        self._note(category, category, how)
        return replacement

    def _redact_embedded_json(self, value: str) -> str | None:
        """A string that is itself a JSON object or array (a serialized tool
        output, a request body): scrub it by structure. None if it isn't JSON;
        the original string if nothing in it needed scrubbing."""
        stripped = value.strip()
        if len(stripped) < 2 or stripped[0] + stripped[-1] not in ("{}", "[]"):
            return None
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            return None
        redacted = self.redact_value(parsed)
        if redacted == parsed:
            return value
        return json.dumps(redacted, ensure_ascii=False)

    def redact_value(self, value, *, key: object | None = None):
        """Recursively scrub JSON-compatible data without dropping structure."""
        category = _sensitive_key_category(key, value) if key is not None else None
        if category and value not in (None, "", [], {}):
            if self._is_placeholder(value):
                return value
            how = f'JSON key "{self._label(str(key))}"'
            if isinstance(value, str) and category == "AUTHORIZATION":
                return self._auth_value(value, how, category)
            return self._alias(category, value, how)
        if isinstance(value, dict):
            return {
                self._redact_text(k) if isinstance(k, str) else k: self.redact_value(v, key=k)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, str):
            embedded = self._redact_embedded_json(value)
            return embedded if embedded is not None else self._redact_text(value)
        return value

    def redact_jsonl(self, text: str, *, name: str = "trace") -> str:
        """Scrub JSONL values by structure; fall back to text for malformed
        lines (counted, and reported as plain-text lines)."""
        out = []
        for lineno, line in enumerate(text.splitlines(keepends=True), 1):
            self.location = f"{name} line {lineno}"
            body = line.rstrip("\r\n")
            newline = line[len(body):]
            if not body.strip():
                out.append(line)
                continue
            try:
                value = json.loads(body)
            except json.JSONDecodeError:
                self.text_lines += 1
                out.append(self._redact_text(body) + newline)
                continue
            self.json_lines += 1
            redacted = self.redact_value(value)
            out.append(json.dumps(redacted, ensure_ascii=False, separators=(",", ":")) + newline)
        self.location = "manifest"
        return "".join(out)

    def summary(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))


def _assignment_category(key: str) -> str:
    norm = _normalise_key(key)
    for needle, category in (
        ("password", "PASSWORD"), ("passwd", "PASSWORD"),
        ("private_key", "PRIVATE_KEY"), ("api_key", "API_KEY"), ("apikey", "API_KEY"),
        ("access_key", "ACCESS_KEY"), ("secret", "SECRET"), ("credential", "CREDENTIAL"),
        ("token", "TOKEN"),
    ):
        if needle in norm:
            return category
    return "SECRET"


def redact(
    text: str,
    *,
    privacy: str = "balanced",
    custom_patterns: list[tuple[int, re.Pattern]] | None = None,
) -> str:
    """Compatibility wrapper for callers that only need the scrubbed text."""
    return TraceRedactor(privacy, custom_patterns).redact_jsonl(text)


def _safe_session_id(value: str) -> str:
    """Require the client-side bucket component to be simple and non-ambiguous."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}", value):
        raise SystemExit(
            "session_id must be 1-192 characters using only letters, digits, "
            "dot, underscore, and hyphen; pass a safe --session-id override"
        )
    return value


def _native_log_name(log_path: Path) -> str:
    """Avoid copying a potentially identifying local filename into shared storage."""
    suffix = log_path.suffix.lower()
    if suffix not in (".jsonl", ".json", ".log", ".txt"):
        suffix = ".log"
    return f"trace{suffix}"


def _prune(value):
    """Drop None values (null == unknown == absent) so manifests stay clean."""
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v is not None}
    return value


def _serialise(fm: dict, body: str) -> str:
    # Emit the frontmatter as JSON — valid YAML, so the backend's yaml.safe_load
    # parses it identically — which keeps this client dependency-free (no PyYAML).
    out = "---\n" + json.dumps(fm, indent=2, ensure_ascii=False) + "\n---\n"
    if body.strip():
        out += "\n" + body.strip("\n") + "\n"
    return out


def build_manifest(
    fields: dict,
    *,
    session_id: str,
    result_ref: str | None,
    native_log_file: str | None = None,
    redaction: dict | None = None,
) -> str:
    fm: dict = {
        "schema_version": 1,
        "adapter_version": ADAPTER_VERSION,
        "harness": fields.get("harness"),
        "session_id": session_id,
    }
    for k in ("model", "started_at", "ended_at"):
        if fields.get(k) is not None:
            fm[k] = fields[k]
    if result_ref:
        fm["result_ref"] = result_ref
    if native_log_file:
        fm["native_log_file"] = native_log_file
    if redaction:
        fm["redaction"] = redaction
    for k in ("usage", "activity", "extensions"):
        pruned = _prune(fields.get(k) or {})
        if pruned:
            fm[k] = pruned
    return _serialise(fm, "")


def _known_harness_complete(harness: str, fields: dict) -> bool:
    usage = fields.get("usage") or {}
    activity = fields.get("activity") or {}
    total = usage.get("total_tokens")
    tools = activity.get("tool_calls")
    return (
        isinstance(total, int)
        and not isinstance(total, bool)
        and isinstance(tools, int)
        and not isinstance(tools, bool)
    )


def _public_session_id(fields: dict, log_path: Path) -> str:
    """The session's public id: the id the harness recorded in the log, else a
    hash of the transcript's location. Never the file name, which can describe
    the work (a stable hash keeps a later --full share in the same place)."""
    if fields.get("session_id"):
        return str(fields["session_id"])
    digest = hashlib.sha256(str(log_path.expanduser().resolve()).encode("utf-8")).hexdigest()
    return f"s-{digest[:16]}"


# ════════════════════════ upload scan + report ════════════════════════

# The scan is a second, independent check over the exact text about to be
# uploaded, read as flat text rather than parsed JSON: JSON-escaped forms,
# JSON nested in strings and truncated fragments the scrubber could not parse
# are all just characters here. It never changes anything. Any hit blocks the
# upload, since it means something got past the scrubber. Lines without a
# trigger word are skipped, which keeps long sessions cheap.
_SCAN_TRIGGERS = (
    "key", "token", "secret", "passw", "credential", "auth", "cookie", "sig", "begin",
    "://", "hf_", "gh", "sk-", "akia", "asia", "xox", "glpat-", "aiza", "npm_", "pypi-", "eyj",
)
_SCAN_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")
# A replaced value followed straight away by more text inside the same token
# (`'<REDACTED:PASSWORD_1>'REMAINDER`): the scrubber cut a value short.
_SCAN_SPLIT_VALUE_RE = re.compile(r"<REDACTED:[A-Z0-9_]+_\d+>\\*[\"'](?P<rest>[A-Za-z0-9][^\s\"'\\,;}\]]*)")
_KNOWN_PREFIXES = (
    "github_pat_", "sk-ant-", "glpat-", "pypi-", "hf_", "ghp_", "gho_", "ghu_", "ghs_",
    "ghr_", "sk-", "AKIA", "ASIA", "xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-", "AIza",
    "npm_", "eyJ",
)


def _mask(value: str) -> str:
    """A value as its length plus a known, non-secret prefix (hf_, sk-, ...)."""
    prefix = next((p for p in _KNOWN_PREFIXES if value.startswith(p)), "")
    return f"{prefix}...({len(value)} chars)"


def _scan_line(line: str, label=lambda name: name):
    """(category, how it was spotted, value) for each credential left in a
    line. `label` sanitizes key names for the report."""
    for category, how, pattern in _providers_in(line):
        for m in pattern.finditer(line):
            yield category, how, m.group(0)
    for m in _SCAN_PRIVATE_KEY_RE.finditer(line):
        yield "PRIVATE_KEY", "private key block", m.group(0)
    for m in _CREDENTIAL_URL_RE.finditer(line):
        if not m.group("password").startswith("<REDACTED:"):
            yield "PASSWORD", "password in a URL", m.group("password")
    for m in _QUERY_SECRET_RE.finditer(line):
        if not m.group("value").startswith("<REDACTED:"):
            yield _normalise_key(m.group("key")).upper(), f"URL param {m.group('key')}", m.group("value")
    for m in _AUTH_HEADER_RE.finditer(line):
        yield "AUTHORIZATION", "Authorization header", m.group("value")
    for m in _COOKIE_HEADER_RE.finditer(line):
        if not m.group("value").strip().startswith("<REDACTED:"):
            yield "COOKIE", "Cookie header", m.group("value").strip()
    # A quoted value after a secret name must be exactly one placeholder: no
    # exemptions for anything that looks like code or a number.
    for m in _QUOTED_SECRET_START_RE.finditer(line):
        end, _close = _quoted_value_end(line, m.end(), m.group("quote"))
        value = line[m.end():end]
        if _ALNUM_RE.search(value) and not _PLACEHOLDER_RE.fullmatch(value):
            key = _full_name(m)
            yield _assignment_category(key), f"assignment {label(key)}", value
    for m in _UNQUOTED_SECRET_RE.finditer(line):
        key = _full_name(m)
        if not m.group("value").startswith("<REDACTED:") and _unquoted_secret(key, m.group("value")):
            yield _assignment_category(key), f"assignment {label(key)}", m.group("value")
    for m in _SCAN_SPLIT_VALUE_RE.finditer(line):
        yield "PARTIAL", "text left after a replaced value", m.group("rest")


def scan_upload(
    files: dict[str, str], custom_patterns=(), privacy: str = "balanced"
) -> tuple[list[dict], int]:
    """Check every line of every file to be uploaded. Returns (hits, lines
    scanned); a hit carries its file, line, category and a masked value."""
    def label(name: str) -> str:
        return _sanitize_label(name, custom_patterns, privacy)

    hits: list[dict] = []
    scanned = 0
    for name, text in files.items():
        for lineno, line in enumerate(text.splitlines(), 1):
            scanned += 1
            low = line.lower()
            if not any(word in low for word in _SCAN_TRIGGERS):
                continue
            for category, how, value in _scan_line(line, label):
                hits.append({
                    "file": name, "line": lineno, "category": category,
                    "how": how, "masked": _mask(value),
                })
    return hits, scanned


_REPORT_MAX_ROWS = 30


def _where(origin: dict) -> str:
    places = origin["where"]
    lines = [p.rsplit(" line ", 1)[1] for p in places if " line " in p]
    parts = []
    if "manifest" in places:
        parts.append("manifest")
    if lines:
        more = " ..." if len(places) > 3 else ""
        parts.append(("line " if len(lines) == 1 else "lines ") + ", ".join(lines[:3]) + more)
    return "; ".join(parts)


def _report_rows(redactor: TraceRedactor) -> list[tuple[str, str, int, str]]:
    """(label, what it was, count, where) per distinct replaced value, in the
    order first seen. Personal-data categories collapse to one row each."""
    rows: list[tuple[str, str, int, str]] = []
    collapsed: dict[str, list[dict]] = {}
    for key, origin in redactor.origins.items():
        if origin["category"] in _COLLAPSED_CATEGORIES:
            collapsed.setdefault(origin["category"], []).append(origin)
            continue
        label = key[len("<REDACTED:"):-1] if key.startswith("<REDACTED:") else key
        rows.append((label, origin["how"], origin["count"], _where(origin)))
    for category, origins in collapsed.items():
        what = f"{len(origins)} distinct {_COLLAPSED_CATEGORIES[category]}"
        rows.append((category, what, sum(o["count"] for o in origins), ""))
    return rows


def format_report(
    redactor: TraceRedactor,
    hits: list[dict] | None,
    scanned: int,
    *,
    scrubbed: dict[str, str] | None = None,
) -> str:
    """The redaction report: what was replaced (by kind, key or pattern and
    line, never the value) and the scan result. `scrubbed` adds the scrubbed
    context around each replacement (--dry-run)."""
    rows = _report_rows(redactor)
    total = sum(count for _, _, count, _ in rows)
    out = [f"scrubbed ({redactor.privacy}): {total} replacement(s), "
           f"{len(redactor.origins)} distinct value(s)"]
    for label, what, count, where in rows[:_REPORT_MAX_ROWS]:
        out.append(f"  {label:<22} {what:<36} {count:>4}x  {where}".rstrip())
    if len(rows) > _REPORT_MAX_ROWS:
        out.append(f"  ... and {len(rows) - _REPORT_MAX_ROWS} more (counted above)")
    if scrubbed:
        out += _context_lines(rows[:_REPORT_MAX_ROWS], redactor, scrubbed)
    lines_note = f"{scanned} line(s) checked"
    if redactor.json_lines or redactor.text_lines:
        lines_note += (f"; trace: {redactor.json_lines} parsed as JSON, "
                       f"{redactor.text_lines} scrubbed as plain text")
    if hits is None:
        out.append(f"scan: DID NOT COMPLETE ({lines_note}) - upload blocked")
    elif not hits:
        out.append(f"scan: clean ({lines_note})")
    else:
        out.append(f"scan: BLOCKED - {len(hits)} possible credential(s) left after scrubbing ({lines_note}):")
        for hit in hits[:20]:
            out.append(f"  {hit['file']} line {hit['line']}: {hit['category']} "
                       f"({hit['how']}) {hit['masked']}")
        if len(hits) > 20:
            out.append(f"  ... and {len(hits) - 20} more")
        out.append(
            "  Nothing was uploaded. Add a regex for it to a --redact-pattern-file and "
            "re-run, use --privacy strict, or share stats only (drop --full)."
        )
    return "\n".join(out)


def _context_lines(rows, redactor: TraceRedactor, scrubbed: dict[str, str]) -> list[str]:
    """About 80 characters of scrubbed text around each placeholder's first
    occurrence in the trace."""
    trace = next(iter(t for name, t in scrubbed.items() if name != "manifest.md"), None)
    if trace is None:
        return []
    trace_lines = trace.splitlines()
    out = ["context (scrubbed):"]
    for label, _what, _count, _where in rows:
        placeholder = f"<REDACTED:{label}>"
        origin = redactor.origins.get(placeholder)
        first = next((p for p in (origin or {}).get("where", []) if " line " in p), None)
        if not first:
            continue
        lineno = int(first.rsplit(" line ", 1)[1])
        line = trace_lines[lineno - 1] if lineno <= len(trace_lines) else ""
        at = line.find(placeholder)
        if at < 0:
            continue
        start, end = max(0, at - 40), min(len(line), at + len(placeholder) + 40)
        out.append(f"  line {lineno}: ...{line[start:end]}...")
    return out if len(out) > 1 else []


def _confirm_or_exit(*, share: str, log_path: Path, yes: bool, privacy: str) -> None:
    """--full needs an explicit yes: typed, or --yes when non-interactive. It
    only confirms the share; it never overrides the session selection or the
    scan, which have already passed by the time this runs."""
    if share != "full" or yes:
        return
    print("\nconfirmation required:")
    print(f"- Full sharing uploads the {privacy}-scrubbed native session log "
          "to your org-readable scratch bucket.")
    print(f"- transcript: {log_path}")
    if not sys.stdin.isatty():
        sys.exit("refusing to continue without --yes in a non-interactive shell")
    answer = input("Continue? Type 'yes' to upload: ").strip().lower()
    if answer != "yes":
        sys.exit("aborted")


def _fetch_v1(backend: str) -> dict | None:
    """GET {backend}/v1 (the self-description: org, collab=slug, central_bucket,
    endpoints). Returns the parsed dict, or None if unreachable."""
    try:
        with urllib.request.urlopen(f"{backend.rstrip('/')}/v1", timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _v1_has_traces(v1: dict) -> bool:
    """Does this backend expose POST /v1/traces? Older deploys predate it."""
    return any(
        isinstance(ep, dict) and ep.get("path") == "/v1/traces" and ep.get("method") == "POST"
        for ep in (v1.get("endpoints") or [])
    )


def _hf_cp(local: str, dest_uri: str) -> None:
    """Upload one file to a bucket via the `hf` CLI (uses your `hf auth login`
    credentials — no Python deps). Progress bars off for clean, scriptable logs."""
    r = subprocess.run(
        ["hf", "buckets", "cp", "--quiet", local, dest_uri],
        env={**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"},
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"`hf buckets cp` failed [{r.returncode}]:\n{(r.stderr or r.stdout).strip()}")


def _harness_arg(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", value):
        raise argparse.ArgumentTypeError("must be a lowercase slug, e.g. claude-code, codex, cursor")
    return value


def main() -> int:
    ap = argparse.ArgumentParser(description="Share a session's stats / trace with the collaboration.")
    ap.add_argument("--harness", type=_harness_arg, default="auto",
                    help="claude-code, codex, or any other harness slug (needs --transcript; "
                         "stats will be partial). Default: auto-detect from the environment/cwd")
    ap.add_argument("--transcript", help="explicit native session log path (else: detected)")
    ap.add_argument("--session-id", help="override the manifest/dest session id")
    ap.add_argument("--full", action="store_true", help="also upload the redacted native session log")
    ap.add_argument("--stats-only", action="store_true", help="deprecated no-op; stats-only is the default")
    ap.add_argument("--privacy", choices=PRIVACY_LEVELS, default="balanced",
                    help="redaction level (default: balanced; applies to --full and manifest strings)")
    ap.add_argument("--redact-pattern-file",
                    help="optional file containing one additional redaction regex per line")
    ap.add_argument("--result-ref", help="filename in results/ this session produced")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"), help="your registered agent_id")
    ap.add_argument("--org", default=os.environ.get("ORG"), help="challenge org")
    ap.add_argument("--slug", default=os.environ.get("COLLAB_SLUG"), help="challenge slug")
    ap.add_argument("--backend", default=os.environ.get("COLLAB_BACKEND") or os.environ.get("API"),
                    help="the backend Space base URL, e.g. https://<org>-<slug>-bucket-sync.hf.space")
    ap.add_argument("--upload-only", action="store_true",
                    help="write the bundle to your scratch bucket and skip POST /v1/traces")
    ap.add_argument("--yes", action="store_true",
                    help="confirm --full in non-interactive use (never overrides the scan)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, manifest and report with scrubbed context; touch nothing")
    args = ap.parse_args()
    if args.full and args.stats_only:
        sys.exit("choose either --full or --stats-only (stats-only is the default)")

    # Auto-discover org/slug from the backend's GET /v1 when not provided, and
    # learn whether this backend even has the trace routes (older deploys don't).
    v1 = _fetch_v1(args.backend) if args.backend else None
    if v1:
        args.org = args.org or v1.get("org")
        args.slug = args.slug or v1.get("collab")
    promote = not args.upload_only
    if promote and v1 is not None and not _v1_has_traces(v1):
        print("note: this backend has no POST /v1/traces yet — saving to your "
              "scratch bucket only (organizers can deploy the trace routes).")
        promote = False

    # 1) locate + parse the native session log (a confident selection or stop)
    if args.transcript:
        log_path = Path(args.transcript).expanduser()
        if not log_path.is_file():
            sys.exit(f"no such transcript: {log_path}")
        harness = args.harness if args.harness != "auto" else _infer_harness(log_path)
        if harness is None:
            sys.exit("could not infer harness from --transcript; pass --harness")
    elif args.harness not in (*KNOWN_HARNESSES, "auto"):
        sys.exit(f"no adapter for --harness {args.harness}; pass --transcript <path> to its session log")
    else:
        harness, log_path = detect(os.getcwd(), args.harness)

    fields = build_fields(harness, log_path)
    if harness in KNOWN_HARNESSES and not _known_harness_complete(harness, fields):
        sys.exit(
            f"{harness} adapter did not produce both usage.total_tokens and "
            "activity.tool_calls; adapter likely needs updating"
        )
    session_id = _safe_session_id(args.session_id or _public_session_id(fields, log_path))
    share = "full" if args.full else "stats"
    native_log_file = _native_log_name(log_path) if share == "full" else None

    # 2) scrub -> scan -> report. Everything uploaded passes through the
    #    scrubber, then the scan reads the exact bytes about to be written.
    patterns = _custom_patterns(args.redact_pattern_file)
    if TraceRedactor(args.privacy, patterns).redact_value(session_id) != session_id:
        sys.exit(
            "session_id matches a sensitive/custom redaction pattern; "
            "pass a non-sensitive --session-id override"
        )
    redactor = TraceRedactor(args.privacy, patterns)
    # session_id and native_log_file are structural identifiers and must match
    # their bucket path. Scrub all descriptive manifest fields around them.
    safe_fields = redactor.redact_value(fields)
    safe_result_ref = redactor.redact_value(args.result_ref) if args.result_ref else None
    log_text = None
    if share == "full":
        log_text = redactor.redact_jsonl(log_path.read_text(encoding="utf-8", errors="replace"))
    manifest = build_manifest(
        safe_fields,
        session_id=session_id,
        result_ref=safe_result_ref,
        native_log_file=native_log_file,
        # Counts as a list, not {"PASSWORD": 1}: a secret name next to a number
        # is exactly what the scan looks for.
        redaction={
            "version": REDACTOR_VERSION,
            "privacy": args.privacy,
            "counts": [{"category": c, "count": n} for c, n in redactor.summary().items()],
        },
    )
    upload = {"manifest.md": manifest}
    if share == "full":
        assert native_log_file is not None and log_text is not None
        upload[native_log_file] = log_text
    try:
        hits, scanned = scan_upload(upload, patterns, args.privacy)
    except Exception as exc:  # a scan that cannot finish never passes for clean
        print(f"warning: the upload scan failed ({type(exc).__name__})")
        hits, scanned = None, 0
    blocked = hits is None or bool(hits)

    # 3) the plan + report
    usage = fields.get("usage") or {}
    activity = fields.get("activity") or {}
    print(f"harness    : {harness}  (adapter v{ADAPTER_VERSION})")
    print(f"log        : {log_path}")
    print(f"session    : {session_id}")
    print(f"share      : {share}  (privacy {args.privacy}, redactor v{REDACTOR_VERSION})")
    print(f"tokens     : {usage.get('total_tokens', 'unknown')}")
    print(f"tool_calls : {activity.get('tool_calls', 'unknown')}")
    if harness not in KNOWN_HARNESSES:
        print(
            f"note       : '{harness}' has no adapter — stats will be partial "
            "(minimal manifest)"
            + (" + native log" if share == "full" else "")
        )
    dest = source = None
    if args.agent_id and args.org and args.slug:
        bucket = f"{args.org}/{args.slug}-{args.agent_id}"
        dest = f"traces/{session_id}"
        source = f"hf://buckets/{bucket}/{dest}"
        print(f"bucket     : {source}")
    print()
    print(format_report(redactor, hits, scanned, scrubbed=upload if args.dry_run else None))

    if args.dry_run:
        print("\n--- manifest.md ---")
        print(manifest)
        if blocked:
            print("(dry run — nothing written; this share would be BLOCKED by the scan)")
            return 1
        print("(dry run — nothing written or uploaded)")
        return 0
    if blocked:
        return 1
    _confirm_or_exit(share=share, log_path=log_path, yes=args.yes, privacy=args.privacy)

    # 3) preflight
    required = [
        (args.agent_id, "--agent-id/AGENT_ID"),
        (args.org, "--org/ORG"),
        (args.slug, "--slug/COLLAB_SLUG"),
    ]
    if promote:
        required.append((args.backend, "--backend/COLLAB_BACKEND/API"))
    for req, name in required:
        if not req:
            sys.exit(f"missing {name}")
    bucket = f"{args.org}/{args.slug}-{args.agent_id}"
    dest = f"traces/{session_id}"
    source = f"hf://buckets/{bucket}/{dest}"

    # 4) write the scanned bytes into YOUR bucket via the `hf` CLI (uses your hf
    #    auth; no Python deps). Nothing unscrubbed or unscanned ever leaves.
    if not shutil.which("hf"):
        sys.exit(
            "the `hf` CLI is required (you already use it for `hf auth login` and "
            "bucket access). Install it with: pip install huggingface_hub"
        )
    with tempfile.TemporaryDirectory() as td:
        for name in sorted(upload, key=lambda n: n != "manifest.md"):  # manifest first
            local = Path(td) / name
            local.write_text(upload[name], encoding="utf-8")
            _hf_cp(str(local), f"hf://buckets/{bucket}/{dest}/{name}")
    print(f"\nwrote {len(upload)} file(s) to {source}")
    if not promote:
        why = "" if args.upload_only else " (this backend has no trace routes yet)"
        print(f"saved to your scratch bucket; skipped POST /v1/traces{why}")
        print(f"verify    : hf buckets list {source}/ -R")
        return 0

    # 5) promote via the backend (identity = bucket; no token on the call)
    body = json.dumps({"source": source, "share": share}).encode("utf-8")
    req = urllib.request.Request(
        f"{args.backend.rstrip('/')}/v1/traces", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            promoted_text = resp.read().decode()
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        code = getattr(e, "code", "—")
        detail = (e.read().decode(errors="replace") if isinstance(e, urllib.error.HTTPError)
                  else str(getattr(e, "reason", e)))
        # The bundle is already in the bucket — a promote failure is partial,
        # not total. Make that legible, but exit non-zero: the share is incomplete.
        print(f"\n✓ bundle uploaded to {source}")
        print(f"⚠ backend promotion failed [{code}]: {detail.strip()[:200]}")
        print("  your trace is safe in your scratch bucket. Re-run with "
              "--upload-only to skip promotion, or tell the organizers if "
              "POST /v1/traces should be available on this collab.")
        return 1
    print(f"promoted: {promoted_text}")
    try:
        promoted = json.loads(promoted_text)
    except json.JSONDecodeError:
        return 0
    detail_url = (
        f"{args.backend.rstrip('/')}/v1/traces/"
        f"{urllib.parse.quote(promoted['agent'])}/"
        f"{urllib.parse.quote(promoted['session_id'])}"
    )
    print(f"trace API  : {detail_url}")
    print(f"trace path : {promoted['path']}")
    central_bucket = (v1 or {}).get("central_bucket")
    if central_bucket:
        print(f"central    : hf://buckets/{central_bucket}/{promoted['path']}")
        if share == "full":
            assert native_log_file is not None
            log_rel = f"{promoted['path']}{native_log_file}"
            viewer = f"https://huggingface.co/buckets/{central_bucket}/{log_rel}"
            print(f"view trace : {viewer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
