"""Per-harness adapters: a harness's NATIVE local session log -> manifest fields.

NOT OpenTelemetry — we parse the harness's own session file (the same files HF's
trace viewer renders). Each adapter returns a dict of manifest fields:
``harness, session_id, model, started_at, ended_at, usage{}, activity{}, extensions{}``.

Cardinal rule: a metric we could not determine is OMITTED (null = unknown); only a
measured zero is 0. Adapters parse defensively — these native formats are unversioned
and drift between harness releases (bump ADAPTER_VERSION when extraction changes).

Verified recipes (2026-06-25): see the agent memory cc-codex-trace-metric-extraction.
  * Claude Code: per-response usage -> SUM across the session.
  * Codex: token_count events are CUMULATIVE -> take the LAST; dedupe tools by call_id.
"""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

ADAPTER_VERSION = 1
KNOWN_HARNESSES = ("claude-code", "codex")


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


# ───────────────────────── Claude Code ─────────────────────────

def claude_code(log_path: Path) -> dict:
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


# ───────────────────────── Codex ─────────────────────────

_CODEX_TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")


def codex(log_path: Path) -> dict:
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


# ───────────────────────── minimal fallback ─────────────────────────

def minimal(log_path: Path, harness: str) -> dict:
    """Unknown harness: ship the raw log + a minimal manifest. No stats — the
    backend records this as `partial` and never blocks participation."""
    return {
        "harness": harness,
        "session_id": log_path.stem,
        "model": None,
        "started_at": None,
        "ended_at": None,
    }


ADAPTERS = {"claude-code": claude_code, "codex": codex}


def build_fields(harness: str, log_path: Path) -> dict:
    fn = ADAPTERS.get(harness)
    return fn(log_path) if fn else minimal(log_path, harness)


# ───────────────────────── detection ─────────────────────────

def _cc_project_dir(cwd: str) -> Path:
    slug = re.sub(r"[/._]", "-", os.path.abspath(cwd))
    return Path.home() / ".claude" / "projects" / slug


def _latest(paths: list[Path]) -> Path | None:
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def detect(cwd: str) -> tuple[str, Path]:
    """Best-effort: the newest Claude Code session for this cwd, else the newest
    Codex rollout. Raises if neither is found (pass --harness/--transcript)."""
    cc_dir = _cc_project_dir(cwd)
    cc = _latest(list(cc_dir.glob("*.jsonl"))) if cc_dir.is_dir() else None
    if cc:
        return "claude-code", cc
    codex_root = Path.home() / ".codex" / "sessions"
    cx = _latest([Path(p) for p in glob.glob(str(codex_root / "**" / "rollout-*.jsonl"), recursive=True)])
    if cx:
        return "codex", cx
    raise SystemExit(
        "could not auto-detect a session log; pass --harness <name> and --transcript <path>"
    )
