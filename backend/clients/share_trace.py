#!/usr/bin/env python3
"""share-trace — share this session's stats (and optionally its full trace).

The deliberate, session-boundary command (see TRACES_DESIGN.md). It parses your
harness's NATIVE session log into a small manifest (token + tool-call stats),
writes the bundle into YOUR OWN scratch bucket, and calls ``POST /v1/traces`` —
the same promote ergonomic as results/artifacts. Identity is your bucket name;
no token rides on the call.

    share-trace                 # FULL: stats + redacted native log -> the library
    share-trace --stats-only    # stats only; no content leaves
    share-trace --raw           # full, skip secret redaction
    share-trace --dry-run       # print the plan + manifest; touch nothing

`full` lets Hugging Face's built-in trace viewer render the native log directly
from the bucket (Claude Code & Codex supported out of the box). Redaction is
best-effort and CLIENT-SIDE — your scratch bucket is org-readable, so content is
scrubbed before it is written there at all.

SELF-CONTAINED, single file by design: agents download just this one file from
the central bucket and run it. Only third-party deps are `yaml` and (lazily, for
upload only) `huggingface_hub` — both already present wherever the `hf` CLI is.
The per-harness adapters are inlined below; keep them in sync with the verified
recipes (memory: cc-codex-trace-metric-extraction).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml


ADAPTER_VERSION = 1
KNOWN_HARNESSES = ("claude-code", "codex")


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


# ════════════════════════ manifest + upload ════════════════════════

# secret scrubbing (best-effort, structure-preserving so the JSON still parses)
_SECRET_PATTERNS = [
    (re.compile(r"hf_[A-Za-z0-9]{20,}"), "<REDACTED>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<REDACTED>"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "<REDACTED>"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "<REDACTED>"),
    (re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?bearer\s+)[A-Za-z0-9._-]+"), r"\1<REDACTED>"),
]


def redact(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _prune(value):
    """Drop None values (null == unknown == absent) so manifests stay clean."""
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v is not None}
    return value


def _serialise(fm: dict, body: str) -> str:
    out = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n"
    if body.strip():
        out += "\n" + body.strip("\n") + "\n"
    return out


def build_manifest(fields: dict, *, session_id: str, result_ref: str | None, summary: str) -> str:
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
    for k in ("usage", "activity", "extensions"):
        pruned = _prune(fields.get(k) or {})
        if pruned:
            fm[k] = pruned
    return _serialise(fm, summary)


def _read_summary(args) -> str:
    if args.summary:
        return args.summary
    if args.summary_file:
        return Path(args.summary_file).expanduser().read_text(encoding="utf-8")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Share a session's stats / trace with the collaboration.")
    ap.add_argument("--harness", choices=[*KNOWN_HARNESSES, "auto"], default="auto",
                    help="default: auto-detect from this cwd (Claude Code, then Codex)")
    ap.add_argument("--transcript", help="explicit native session log path (else: detected)")
    ap.add_argument("--session-id", help="override the manifest/dest session id")
    ap.add_argument("--stats-only", action="store_true", help="share numbers only; no content leaves")
    ap.add_argument("--raw", action="store_true", help="full, but skip secret redaction (upload as-is)")
    ap.add_argument("--summary", help="the 'what I did' body (prose; recommended for full traces)")
    ap.add_argument("--summary-file", help="read the summary body from a file")
    ap.add_argument("--result-ref", help="filename in results/ this session produced")
    ap.add_argument("--agent-id", default=os.environ.get("AGENT_ID"), help="your registered agent_id")
    ap.add_argument("--org", default=os.environ.get("ORG"), help="challenge org")
    ap.add_argument("--slug", default=os.environ.get("COLLAB_SLUG"), help="challenge slug")
    ap.add_argument("--backend", default=os.environ.get("COLLAB_BACKEND"),
                    help="the backend Space base URL, e.g. https://<org>-<slug>-bucket-sync.hf.space")
    ap.add_argument("--dry-run", action="store_true", help="print the plan + manifest; touch nothing")
    args = ap.parse_args()

    # 1) locate + parse the native session log
    if args.transcript:
        harness = args.harness if args.harness != "auto" else "claude-code"
        log_path = Path(args.transcript).expanduser()
        if not log_path.is_file():
            sys.exit(f"no such transcript: {log_path}")
    elif args.harness != "auto":
        harness = args.harness
        _, log_path = detect(os.getcwd())  # detect to find the path; harness forced
    else:
        harness, log_path = detect(os.getcwd())

    fields = build_fields(harness, log_path)
    session_id = args.session_id or str(fields.get("session_id") or log_path.stem)
    share = "stats" if args.stats_only else "full"
    manifest = build_manifest(fields, session_id=session_id, result_ref=args.result_ref,
                              summary=_read_summary(args))

    # 2) the plan
    usage = fields.get("usage") or {}
    activity = fields.get("activity") or {}
    print(f"harness    : {harness}  (adapter v{ADAPTER_VERSION})")
    print(f"log        : {log_path}")
    print(f"session    : {session_id}")
    print(f"share      : {share}" + ("  [redaction OFF]" if args.raw else ""))
    print(f"tokens     : {usage.get('total_tokens', 'unknown')}")
    print(f"tool_calls : {activity.get('tool_calls', 'unknown')}")
    if harness not in KNOWN_HARNESSES:
        print(f"note       : '{harness}' has no adapter — shipping raw log + minimal manifest (partial)")

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

    # 3) preflight
    for req, name in [(args.agent_id, "--agent-id/AGENT_ID"), (args.org, "--org/ORG"),
                      (args.slug, "--slug/COLLAB_SLUG"), (args.backend, "--backend/COLLAB_BACKEND")]:
        if not req:
            sys.exit(f"missing {name}")
    bucket = f"{args.org}/{args.slug}-{args.agent_id}"

    # 4) write the bundle into YOUR bucket (your own token), redacting content first
    from huggingface_hub import batch_bucket_files, create_bucket, get_token
    from huggingface_hub.errors import HfHubHTTPError

    token = os.environ.get("HF_TOKEN") or get_token()
    if not token:
        sys.exit("no HF token; set HF_TOKEN or run `hf auth login`")
    try:
        create_bucket(bucket, private=False, exist_ok=True, token=token)
    except HfHubHTTPError as e:
        print(f"  note: create_bucket({bucket}) -> {e} (continuing — usually already exists)")

    adds: list[tuple[bytes, str]] = [(manifest.encode("utf-8"), f"{dest}/manifest.md")]
    if share == "full":
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        if not args.raw:
            log_text = redact(log_text)
        adds.append((log_text.encode("utf-8"), f"{dest}/{log_path.name}"))
    batch_bucket_files(bucket, add=adds, token=token)
    print(f"\nwrote {len(adds)} file(s) to {source}")

    # 5) promote via the backend (identity = bucket; no token on the call)
    body = json.dumps({"source": source, "share": share}).encode("utf-8")
    req = urllib.request.Request(
        f"{args.backend.rstrip('/')}/v1/traces", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"promoted: {resp.read().decode()}")
    except urllib.error.HTTPError as e:
        sys.exit(f"promote failed [{e.code}]: {e.read().decode(errors='replace')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
