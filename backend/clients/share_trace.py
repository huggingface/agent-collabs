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
  * The DEFAULT (`stats`) uploads only token + tool-call COUNTS — no prompts, no
    code, no file contents. `--full` additionally uploads the native session log
    with credentials and personal identifiers pseudonymized CLIENT-SIDE first.
  * Destination is YOUR OWN Hugging Face scratch bucket in the challenge org (via
    the `hf` CLI you already use), then the collab's own API — never an arbitrary
    external host. It uses your existing `hf` login; the script reads/transmits
    no token, and manifest strings pass through the same scrubber.
  * `--dry-run` prints the manifest and a typed redaction summary without writing
    or uploading anything — run it first to verify.

    python share_trace.py                 # stats only; no content leaves (the floor)
    python share_trace.py --upload-only   # write to scratch bucket; skip backend promotion
    python share_trace.py --full --yes    # FULL: stats + balanced-redacted log -> library
    python share_trace.py --full --privacy secrets  # credentials only; preserve PII
    python share_trace.py --full --privacy strict   # also pseudonymize hosts + IPs
    python share_trace.py --full --raw    # UNSAFE: full, skip all redaction
    python share_trace.py --dry-run       # print the plan + manifest; touch nothing

`full` lets Hugging Face's built-in trace viewer render the native log directly
from the bucket (Claude Code & Codex supported out of the box). Redaction parses
JSONL and makes surgical, typed substitutions that preserve prompts, responses,
commands, tool structure, and relative paths. It is still best-effort and cannot
infer whether ordinary task prose or source code is confidential. Your scratch
bucket is org-readable, so content is scrubbed before it is written there at all.
`--full` needs confirmation; pass `--yes` for non-interactive / agent runs.

Auto-detection follows the harness that INVOKES this script (from its env —
Claude Code's CLAUDE_CODE_SESSION_ID pins the *exact* session), so multiple
agents sharing one directory are never cross-attributed. Override with
`--harness` / `--transcript`.

SELF-CONTAINED + DEPENDENCY-FREE by design: download this one file and run it
under ANY `python3` — no `pip install`. The frontmatter is emitted as JSON
(which is valid YAML, so the backend parses it identically) and the upload
shells out to the `hf` CLI (which you already use for `hf auth login`). Org/slug
are auto-discovered from the backend's `GET /v1`, so you only need `--backend`
(or `COLLAB_BACKEND`) and your `--agent-id`. The per-harness adapters are inlined
below; keep them in sync with the verified recipes (memory:
cc-codex-trace-metric-extraction).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ADAPTER_VERSION = 1
REDACTOR_VERSION = 2
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
    """~/.claude/projects/<slug>/<session_id>.jsonl — per-response usage is SUMMED."""
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
    saw_usage = False
    tools: dict[str, int] = {}
    api_requests = 0
    model = None
    session_id = log_path.stem
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
        api_requests += 1
        msg = rec.get("message") or {}
        if msg.get("model"):
            model = msg["model"]
        u = msg.get("usage") or {}
        if u:
            saw_usage = True
            usage["input_tokens"] += _int(u.get("input_tokens")) or 0
            usage["output_tokens"] += _int(u.get("output_tokens")) or 0
            usage["cache_read_tokens"] += _int(u.get("cache_read_input_tokens")) or 0
            usage["cache_creation_tokens"] += _int(u.get("cache_creation_input_tokens")) or 0
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or "?"
                tools[name] = tools.get(name, 0) + 1

    fields: dict = {
        "harness": "claude-code",
        "session_id": session_id,
        "model": model,
        "started_at": first_ts,
        "ended_at": last_ts,
        "activity": {"tool_calls": sum(tools.values()), "tool_calls_by_name": tools},
        "extensions": {"api_requests": api_requests},
    }
    if saw_usage:
        usage["total_tokens"] = sum(usage.values())
        fields["usage"] = usage
    return fields


_CODEX_TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")


def adapter_codex(log_path: Path) -> dict:
    """~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl — token_count is CUMULATIVE
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
        "session_id": session_id or log_path.stem,
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
        "session_id": log_path.stem,
        "model": None,
        "started_at": None,
        "ended_at": None,
    }


ADAPTERS = {"claude-code": adapter_claude_code, "codex": adapter_codex}


def build_fields(harness: str, log_path: Path) -> dict:
    fn = ADAPTERS.get(harness)
    return fn(log_path) if fn else adapter_minimal(log_path, harness)


def _cc_project_dir(cwd: str) -> Path:
    slug = re.sub(r"[/._]", "-", os.path.abspath(cwd))
    return Path.home() / ".claude" / "projects" / slug


def _latest(paths: list[Path]) -> Path | None:
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _detect_claude_code(cwd: str) -> Path | None:
    cc_dir = _cc_project_dir(cwd)
    return _latest(list(cc_dir.glob("*.jsonl"))) if cc_dir.is_dir() else None


def _codex_logs() -> list[Path]:
    codex_root = Path.home() / ".codex" / "sessions"
    return sorted(
        [Path(p) for p in glob.glob(str(codex_root / "**" / "rollout-*.jsonl"), recursive=True)],
        key=lambda p: p.stat().st_mtime if p.is_file() else 0,
        reverse=True,
    )


def _mentions_cwd(value, cwd: str) -> bool:
    cwd_abs = os.path.abspath(cwd)
    if isinstance(value, str):
        if cwd_abs in value:
            return True
        try:
            return os.path.abspath(os.path.expanduser(value)) == cwd_abs
        except ValueError:
            return False
    if isinstance(value, dict):
        return any(_mentions_cwd(v, cwd_abs) for v in value.values())
    if isinstance(value, list):
        return any(_mentions_cwd(v, cwd_abs) for v in value)
    return False


def _codex_matches_cwd(path: Path, cwd: str) -> bool:
    for i, rec in enumerate(_jsonl(path)):
        if i >= 250:
            break
        if _mentions_cwd(rec, cwd):
            return True
    return False


def _detect_codex(cwd: str) -> tuple[Path | None, bool]:
    logs = _codex_logs()
    for path in logs:
        if _codex_matches_cwd(path, cwd):
            return path, False
    return (logs[0], True) if logs else (None, False)


def _infer_harness(log_path: Path) -> str | None:
    s = str(log_path)
    if "/.codex/sessions/" in s or log_path.name.startswith("rollout-"):
        return "codex"
    if "/.claude/projects/" in s:
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


def _cc_session_count(cwd: str) -> int:
    d = _cc_project_dir(cwd)
    return len(list(d.glob("*.jsonl"))) if d.is_dir() else 0


def detect(cwd: str, harness: str) -> tuple[str, Path, bool]:
    """Detect the native session log of the agent INVOKING this script.

    Anchored on the invoking harness's environment (see _running_harness) so
    agents sharing a directory aren't cross-attributed. Returns
    (harness, path, uncertain); `uncertain` asks main() to confirm when the
    exact session could not be pinned.
    """
    env_harness, env_sid = _running_harness()

    if harness == "auto":
        if env_harness:
            harness = env_harness
        else:
            # The env doesn't say who's running. Use the sole candidate; if both
            # harnesses have one, refuse rather than guess (the cross-attrib bug).
            cc = _detect_claude_code(cwd)
            cx, cx_fallback = _detect_codex(cwd)
            if cc and cx:
                raise SystemExit(
                    "multiple harnesses have a session for this directory and the "
                    "environment doesn't identify the running agent — pass "
                    "--harness claude-code|codex (or --transcript <path>)."
                )
            if cc:
                return "claude-code", cc, _cc_session_count(cwd) > 1
            if cx:
                return "codex", cx, cx_fallback
            raise SystemExit(
                "could not auto-detect a session log; pass --harness and --transcript"
            )
    elif env_harness and env_harness != harness:
        print(f"warning: --harness {harness}, but this looks like a {env_harness} "
              "session from the environment; proceeding as requested.")

    if harness == "claude-code":
        if env_sid:
            pinned = _cc_session_log(cwd, env_sid)
            if pinned:
                return "claude-code", pinned, False  # exact session — no ambiguity
            raise SystemExit(
                f"CLAUDE_CODE_SESSION_ID={env_sid} has no transcript under "
                f"{_cc_project_dir(cwd)}; refusing to guess another session. "
                "Pass --transcript <path> if the transcript lives elsewhere."
            )
        cc = _detect_claude_code(cwd)
        if cc:
            return "claude-code", cc, (env_sid is None and _cc_session_count(cwd) > 1)
        raise SystemExit("could not find a Claude Code session for this cwd; pass --transcript")

    if harness == "codex":
        cx, cx_fallback = _detect_codex(cwd)
        if cx:
            return "codex", cx, cx_fallback
        raise SystemExit("could not find a Codex rollout; pass --transcript")

    raise SystemExit(f"unknown harness: {harness!r}")


# ════════════════════════ manifest + upload ════════════════════════

# Best-effort, structure-preserving scrubber. Provider signatures catch secrets
# wherever they appear; context patterns catch opaque values next to sensitive
# keys. Deliberately avoid generic entropy detection: traces legitimately contain
# commit SHAs, call IDs, hashes, and generated identifiers.
_PROVIDER_SECRET_PATTERNS = [
    ("HF_TOKEN", re.compile(r"(?<![A-Za-z0-9_])hf_[A-Za-z0-9]{20,}(?![A-Za-z0-9_])")),
    (
        "GITHUB_TOKEN",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,})(?![A-Za-z0-9_])"
        ),
    ),
    ("SK_TOKEN", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("AWS_ACCESS_KEY", re.compile(r"(?<![0-9A-Z])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])")),
    ("SLACK_TOKEN", re.compile(r"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{10,}(?![A-Za-z0-9-])")),
    ("GITLAB_TOKEN", re.compile(r"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("GOOGLE_API_KEY", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])")),
    ("NPM_TOKEN", re.compile(r"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{36}(?![A-Za-z0-9_])")),
    ("PYPI_TOKEN", re.compile(r"(?<![A-Za-z0-9_-])pypi-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    (
        "JWT",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
            r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
        ),
    ),
]

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
    r"(?P<value>[^\"'\r\n]+)"
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?i)(?P<scheme>\b(?:https?|postgres(?:ql)?|mysql|mariadb|"
    r"mongodb(?:\+srv)?|redis|amqps?|ssh|sftp|ftp)://)"
    r"(?P<username>[^:@/\s]+):(?P<password>[^@/\s]+)@"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)(?P<prefix>[?&](?P<key>access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|token|secret|password|sig|signature)=)"
    r"(?P<value>[^&#\s\"']+)"
)
_TEXT_SECRET_KEY = (
    r"password|passwd|pwd|secret|secret[_-]?key|client[_-]?secret|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session[_-]?token|aws[_-]?secret[_-]?access[_-]?key"
)
_QUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<key>\b(?:{_TEXT_SECRET_KEY})\b)"
    r"(?P<sep>\s*[:=]\s*)(?P<quote>[\"'])"
    r"(?P<value>[^\r\n]*?)(?P=quote)"
)
_UNQUOTED_SECRET_RE = re.compile(
    rf"(?i)(?P<key>\b(?:{_TEXT_SECRET_KEY})\b)"
    r"(?P<sep>\s*[:=]\s*)(?![\"'])(?P<value>[^\s,;}\]]+)"
)
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


def _normalise_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")


def _custom_patterns(path: str | None) -> list[re.Pattern]:
    """Load one non-empty regex per line from an explicitly supplied file."""
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
        out.append(compiled)
    return out


class TraceRedactor:
    """JSON-aware scrubber with stable, typed aliases for one trace."""

    def __init__(
        self,
        privacy: str = "balanced",
        custom_patterns: list[re.Pattern] | None = None,
    ):
        if privacy not in PRIVACY_LEVELS:
            raise ValueError(f"unknown privacy level: {privacy!r}")
        self.privacy = privacy
        self.custom_patterns = custom_patterns or []
        self._aliases: dict[str, tuple[str, str]] = {}
        self._next: dict[str, int] = {}
        self._counts: dict[str, int] = {}

    @staticmethod
    def _is_placeholder(value: object) -> bool:
        return isinstance(value, str) and bool(_PLACEHOLDER_RE.fullmatch(value))

    def _alias(self, category: str, value: object) -> str:
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
        self._counts[actual_category] = self._counts.get(actual_category, 0) + 1
        return placeholder

    def _auth_value(self, value: str, category: str = "AUTHORIZATION") -> str:
        if self._is_placeholder(value):
            return value
        match = re.fullmatch(
            r"(?is)\s*(bearer|basic|token|api[-_]?key)\s+(.+?)\s*", value
        )
        if not match:
            return self._alias(category, value)
        scheme, credential = match.groups()
        kind = {
            "bearer": "BEARER_TOKEN",
            "basic": "BASIC_CREDENTIAL",
            "token": "AUTH_TOKEN",
            "apikey": "API_KEY",
            "api-key": "API_KEY",
            "api_key": "API_KEY",
        }[scheme.lower()]
        return f"{scheme} {self._alias(kind, credential)}"

    def _redact_text(self, text: str) -> str:
        if not text:
            return text

        text = _PRIVATE_KEY_RE.sub(
            lambda m: self._alias("PRIVATE_KEY", m.group(0)), text
        )

        def auth_repl(match: re.Match) -> str:
            scheme = match.group("scheme")
            value = match.group("value")
            return match.group("prefix") + self._auth_value(f"{scheme} {value}")

        text = _AUTH_HEADER_RE.sub(auth_repl, text)
        text = _COOKIE_HEADER_RE.sub(
            lambda m: m.group("prefix") + self._alias("COOKIE", m.group("value").rstrip())
            + m.group("value")[len(m.group("value").rstrip()):],
            text,
        )
        text = _CREDENTIAL_URL_RE.sub(
            lambda m: (
                m.group("scheme")
                + self._alias("USERNAME", m.group("username"))
                + ":"
                + self._alias("PASSWORD", m.group("password"))
                + "@"
            ),
            text,
        )
        text = _QUERY_SECRET_RE.sub(
            lambda m: m.group("prefix")
            + self._alias(_normalise_key(m.group("key")), m.group("value")),
            text,
        )

        def quoted_repl(match: re.Match) -> str:
            value = match.group("value")
            if self._is_placeholder(value):
                return match.group(0)
            return (
                match.group("key")
                + match.group("sep")
                + match.group("quote")
                + self._alias(_normalise_key(match.group("key")), value)
                + match.group("quote")
            )

        def unquoted_repl(match: re.Match) -> str:
            value = match.group("value")
            if self._is_placeholder(value):
                return match.group(0)
            return (
                match.group("key")
                + match.group("sep")
                + self._alias(_normalise_key(match.group("key")), value)
            )

        text = _QUOTED_SECRET_RE.sub(quoted_repl, text)
        text = _UNQUOTED_SECRET_RE.sub(unquoted_repl, text)
        for category, pattern in _PROVIDER_SECRET_PATTERNS:
            text = pattern.sub(lambda m, c=category: self._alias(c, m.group(0)), text)
        for pattern in self.custom_patterns:
            text = pattern.sub(lambda m: self._alias("CUSTOM", m.group(0)), text)

        if self.privacy in ("balanced", "strict"):
            text = _POSIX_HOME_RE.sub(
                lambda m: self._count_static("HOME_PATH", "$HOME"), text
            )
            text = _WINDOWS_HOME_RE.sub(
                lambda m: self._count_static("HOME_PATH", "$HOME"), text
            )
            text = _EMAIL_RE.sub(lambda m: self._alias("EMAIL", m.group(0)), text)

        if self.privacy == "strict":
            text = _URL_HOST_RE.sub(
                lambda m: m.group("prefix") + self._alias("HOST", m.group("host")),
                text,
            )

            def ipv4_repl(match: re.Match) -> str:
                value = match.group(0)
                if any(int(part) > 255 for part in value.split(".")):
                    return value
                return self._alias("IP", value)

            text = _IPV4_RE.sub(ipv4_repl, text)
        return text

    def _count_static(self, category: str, replacement: str) -> str:
        self._counts[category] = self._counts.get(category, 0) + 1
        return replacement

    def redact_value(self, value, *, key: object | None = None):
        """Recursively scrub JSON-compatible data without dropping structure."""
        category = _SENSITIVE_KEYS.get(_normalise_key(key)) if key is not None else None
        if category and value not in (None, "", [], {}):
            if self._is_placeholder(value):
                return value
            if isinstance(value, str) and category == "AUTHORIZATION":
                return self._auth_value(value, category)
            return self._alias(category, value)
        if isinstance(value, dict):
            return {
                self._redact_text(k) if isinstance(k, str) else k: self.redact_value(v, key=k)
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, str):
            return self._redact_text(value)
        return value

    def redact_jsonl(self, text: str) -> str:
        """Scrub JSONL values by structure; fall back to text for malformed lines."""
        out = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            newline = line[len(body):]
            if not body.strip():
                out.append(line)
                continue
            try:
                value = json.loads(body)
            except json.JSONDecodeError:
                out.append(self._redact_text(body) + newline)
                continue
            redacted = self.redact_value(value)
            out.append(json.dumps(redacted, ensure_ascii=False, separators=(",", ":")) + newline)
        return "".join(out)

    def summary(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))


def redact(
    text: str,
    *,
    privacy: str = "balanced",
    custom_patterns: list[re.Pattern] | None = None,
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


def _confirm_or_exit(
    *,
    share: str,
    log_path: Path,
    uncertain: bool,
    yes: bool,
    raw: bool,
    privacy: str,
) -> None:
    reasons = []
    if uncertain:
        reasons.append("Could not pin the exact invoking session — selection fell back to "
                       "the newest log for this directory; confirm it is the right one.")
    if share == "full":
        if raw:
            reasons.append(
                "Full --raw sharing uploads the UNREDACTED native session log "
                "to your org-readable scratch bucket."
            )
        else:
            reasons.append(
                f"Full sharing uploads the {privacy}-redacted native session log "
                "to your org-readable scratch bucket."
            )
    if not reasons or yes:
        return
    print("\nconfirmation required:")
    for reason in reasons:
        print(f"- {reason}")
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Share a session's stats / trace with the collaboration.")
    ap.add_argument("--harness", choices=[*KNOWN_HARNESSES, "auto"], default="auto",
                    help="default: auto-detect from this cwd (Claude Code, then Codex)")
    ap.add_argument("--transcript", help="explicit native session log path (else: detected)")
    ap.add_argument("--session-id", help="override the manifest/dest session id")
    ap.add_argument("--full", action="store_true", help="also upload the redacted native session log")
    ap.add_argument("--stats-only", action="store_true", help="deprecated no-op; stats-only is the default")
    ap.add_argument("--raw", action="store_true",
                    help="UNSAFE: with --full, skip transcript/manifest redaction")
    ap.add_argument("--privacy", choices=PRIVACY_LEVELS, default="balanced",
                    help="redaction level (default: balanced; applies to --full and manifest strings)")
    ap.add_argument("--redact-pattern-file",
                    help="optional file containing one additional redaction regex per line")
    ap.add_argument("--result-ref", help="filename in results/ this session produced")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"), help="your registered agent_id")
    ap.add_argument("--org", default=os.environ.get("ORG"), help="challenge org")
    ap.add_argument("--slug", default=os.environ.get("COLLAB_SLUG"), help="challenge slug")
    ap.add_argument("--backend", default=os.environ.get("COLLAB_BACKEND"),
                    help="the backend Space base URL, e.g. https://<org>-<slug>-bucket-sync.hf.space")
    ap.add_argument("--upload-only", action="store_true",
                    help="write the bundle to your scratch bucket and skip POST /v1/traces")
    ap.add_argument("--yes", action="store_true", help="confirm --full/global Codex fallback in non-interactive use")
    ap.add_argument("--dry-run", action="store_true", help="print the plan + manifest; touch nothing")
    args = ap.parse_args()
    if args.full and args.stats_only:
        sys.exit("choose either --full or --stats-only (stats-only is the default)")
    if args.raw and not args.full:
        sys.exit("--raw only applies with --full")
    if args.raw and args.redact_pattern_file:
        sys.exit("--redact-pattern-file cannot be combined with --raw")

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

    # 1) locate + parse the native session log
    uncertain = False
    if args.transcript:
        log_path = Path(args.transcript).expanduser()
        if not log_path.is_file():
            sys.exit(f"no such transcript: {log_path}")
        harness = args.harness if args.harness != "auto" else _infer_harness(log_path)
        if harness is None:
            sys.exit("could not infer harness from --transcript; pass --harness")
    else:
        harness, log_path, uncertain = detect(os.getcwd(), args.harness)

    fields = build_fields(harness, log_path)
    if harness in KNOWN_HARNESSES and not _known_harness_complete(harness, fields):
        sys.exit(
            f"{harness} adapter did not produce both usage.total_tokens and "
            "activity.tool_calls; adapter likely needs updating"
        )
    session_id = _safe_session_id(
        args.session_id or str(fields.get("session_id") or log_path.stem)
    )
    share = "full" if args.full else "stats"
    native_log_file = _native_log_name(log_path) if share == "full" else None
    log_text = None
    if args.raw:
        safe_fields = fields
        safe_result_ref = args.result_ref
        redaction_meta = {
            "version": REDACTOR_VERSION,
            "privacy": "raw",
            "counts": {},
        }
        if share == "full":
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
    else:
        patterns = _custom_patterns(args.redact_pattern_file)
        identifier_probe = TraceRedactor(args.privacy, patterns)
        if identifier_probe.redact_value(session_id) != session_id:
            sys.exit(
                "session_id matches a sensitive/custom redaction pattern; "
                "pass a non-sensitive --session-id override"
            )
        redactor = TraceRedactor(args.privacy, patterns)
        # session_id and native_log_file are structural identifiers and must match
        # their bucket path. Scrub all descriptive manifest fields around them.
        safe_fields = redactor.redact_value(fields)
        safe_result_ref = (
            redactor.redact_value(args.result_ref) if args.result_ref else None
        )
        if share == "full":
            log_text = redactor.redact_jsonl(
                log_path.read_text(encoding="utf-8", errors="replace")
            )
        redaction_meta = {
            "version": REDACTOR_VERSION,
            "privacy": args.privacy,
            "counts": redactor.summary(),
        }
    manifest = build_manifest(
        safe_fields,
        session_id=session_id,
        result_ref=safe_result_ref,
        native_log_file=native_log_file,
        redaction=redaction_meta,
    )

    # 2) the plan
    usage = fields.get("usage") or {}
    activity = fields.get("activity") or {}
    print(f"harness    : {harness}  (adapter v{ADAPTER_VERSION})")
    print(f"log        : {log_path}")
    print(f"session    : {session_id}")
    print(f"share      : {share}" + ("  [redaction OFF]" if args.raw else ""))
    if not args.raw:
        print(f"privacy    : {args.privacy}  (redactor v{REDACTOR_VERSION})")
        counts = redaction_meta["counts"]
        summary = ", ".join(f"{kind}={count}" for kind, count in counts.items())
        print(f"redactions : {summary or 'none'}")
    if uncertain:
        print("selection  : newest log for this cwd (exact session not pinned — verify it's yours)")
    print(f"tokens     : {usage.get('total_tokens', 'unknown')}")
    print(f"tool_calls : {activity.get('tool_calls', 'unknown')}")
    if harness not in KNOWN_HARNESSES:
        print(
            f"note       : '{harness}' has no adapter — shipping a minimal "
            "manifest (partial)"
            + (" + native log" if share == "full" else "")
        )

    dest = source = None
    if args.agent_id and args.org and args.slug:
        bucket = f"{args.org}/{args.slug}-{args.agent_id}"
        dest = f"traces/{session_id}"
        source = f"hf://buckets/{bucket}/{dest}"
        print(f"bucket     : {source}")
    print("\n--- manifest.md ---")
    print(manifest)

    if args.dry_run:
        print("(dry run — nothing written or uploaded)")
        return 0
    _confirm_or_exit(
        share=share,
        log_path=log_path,
        uncertain=uncertain,
        yes=args.yes,
        raw=args.raw,
        privacy=args.privacy,
    )

    # 3) preflight
    required = [
        (args.agent_id, "--agent-id/AGENT_ID"),
        (args.org, "--org/ORG"),
        (args.slug, "--slug/COLLAB_SLUG"),
    ]
    if promote:
        required.append((args.backend, "--backend/COLLAB_BACKEND"))
    for req, name in required:
        if not req:
            sys.exit(f"missing {name}")
    bucket = f"{args.org}/{args.slug}-{args.agent_id}"
    dest = f"traces/{session_id}"
    source = f"hf://buckets/{bucket}/{dest}"

    # 4) write the bundle into YOUR bucket via the `hf` CLI (uses your hf auth;
    #    no Python deps). Content is redacted client-side before it ever leaves.
    if not shutil.which("hf"):
        sys.exit(
            "the `hf` CLI is required (you already use it for `hf auth login` and "
            "bucket access). Install it with: pip install huggingface_hub"
        )
    with tempfile.TemporaryDirectory() as td:
        man = Path(td) / "manifest.md"
        man.write_text(manifest, encoding="utf-8")
        _hf_cp(str(man), f"hf://buckets/{bucket}/{dest}/manifest.md")
        n_files = 1
        if share == "full":
            assert log_text is not None
            assert native_log_file is not None
            logf = Path(td) / native_log_file
            logf.write_text(log_text, encoding="utf-8")
            _hf_cp(str(logf), f"hf://buckets/{bucket}/{dest}/{native_log_file}")
            n_files = 2
    print(f"\nwrote {n_files} file(s) to {source}")
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
        # not total. Make that legible and exit 0 (the upload succeeded).
        print(f"\n✓ bundle uploaded to {source}")
        print(f"⚠ backend promotion failed [{code}]: {detail.strip()[:200]}")
        print("  your trace is safe in your scratch bucket. Re-run with "
              "--upload-only to skip promotion, or tell the organizers if "
              "POST /v1/traces should be available on this collab.")
        return 0
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
