"""End-to-end tests for clients/collab_watch.sh (WATCH_DESIGN §5).

The script is driven as a real subprocess (`sh collab_watch.sh ...`) against a
STUB HTTP server that speaks the §4.4 response contract: compact JSON, a
top-level ``cursor``, and a ``watch`` block whenever ``wait>0`` was requested.

Why a stub instead of the FastAPI app (which is how eq2 tested this): the
client contract is what is under test here, and half of it only exists in
conditions a healthy server will not produce on demand — a 4xx on a typo'd
handle, ten 5xx in a row, an instantly-empty *degraded* answer, a page whose
record content is deliberately shaped like a cursor. The stub makes each of
those a one-line setting, keeps the suite honest about what the *client* does,
and leaves it runnable while the server side is still being written.

Waits are kept tiny (COLLAB_WATCH_WAIT=1) and retry backoff is switched off
(COLLAB_WATCH_BACKOFF=0) so the whole module stays a few seconds of wall clock.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest


SCRIPT = str(Path(__file__).resolve().parent.parent / "clients" / "collab_watch.sh")

POISON = "99999999-235959-999_zzz.md"  # sorts after every real filename


# ── stub server ───────────────────────────────────────────────────────


@dataclass
class Msg:
    filename: str
    frontmatter: dict[str, Any]
    body: str


@dataclass
class Stub:
    """A programmable stand-in for the read side of the collab API."""

    base_url: str = ""
    messages: list[Msg] = field(default_factory=list)
    requests: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # misbehaviour knobs
    http_status: int | None = None        # force this status on every request
    error_body: bytes = b'{"error":{"code":"BOOM","message":"boom"}}'
    # Answer wait>0 instantly with this status instead of holding the request.
    # "degraded"/"evicted" are the over-cap answers; "timeout" is the truthful
    # instant answer of a server whose effective wait budget is 0
    # (LONGPOLL_MAX_WAIT_S=0), which the client must still pace against.
    instant_status: str | None = None
    omit_cursor: bool = False             # pretend the server predates §4.4
    matched_override: int | None = None   # lie about `matched`
    grow_polls: int = 0                   # land fresh mail during the next N polls
    _n: int = 0

    def add(self, body: str = "ping @agent-a", author: str = "agent-b",
            frontmatter: dict[str, Any] | None = None) -> str:
        with self.lock:
            self._n += 1
            filename = f"20260728-12{self._n // 60:02d}{self._n % 60:02d}-000_{author}.md"
            fm = {"type": "agent", "agent": author, "via": "raw"}
            fm.update(frontmatter or {})
            self.messages.append(Msg(filename, fm, body))
        return filename

    def select(self, after: str | None, order: str, limit: int) -> tuple[list[Msg], bool]:
        with self.lock:
            msgs = sorted(
                (m for m in self.messages if after is None or m.filename > after),
                key=lambda m: m.filename,
            )
        if order == "desc":
            msgs.reverse()
        truncated = bool(limit) and len(msgs) > limit
        if truncated:
            msgs = msgs[:limit]
        return msgs, truncated

    def page(self, items: list[Msg], truncated: bool, wait: float,
             waited_ms: int, status: str) -> bytes:
        """The §4.4 shape, in field order, compact — as Starlette emits it."""
        with self.lock:
            total = len(self.messages)
        doc: dict[str, Any] = {
            "count": total,
            # `matched` is deliberately NOT cursor filtered: it is the field a
            # wrapper must never mistake for an unread count.
            "matched": total if self.matched_override is None else self.matched_override,
            "items": [
                {"filename": m.filename, "frontmatter": m.frontmatter, "body": m.body}
                for m in items
            ],
            "next": items[-1].filename if truncated else None,
        }
        if not self.omit_cursor:
            doc["cursor"] = max((m.filename for m in items), default=None)
        if wait > 0:
            doc["watch"] = {"status": status, "waited_ms": waited_ms}
        return json.dumps(doc, separators=(",", ":")).encode()

    def paths(self) -> list[str]:
        with self.lock:
            return [p for p, _q in self.requests]

    def last_query(self) -> dict[str, str]:
        with self.lock:
            return dict(self.requests[-1][1])

    def n_requests(self) -> int:
        with self.lock:
            return len(self.requests)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stub: Stub

    def log_message(self, *_args):  # keep pytest output clean
        pass

    def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        stub = self.stub
        with stub.lock:
            stub.requests.append((parsed.path, query))
            forced, err = stub.http_status, stub.error_body
            grow = stub.grow_polls > 0
            if grow:
                stub.grow_polls -= 1
        if forced:
            self._send(forced, err)
            return
        if grow:
            # Ordinary traffic landing *while* a page is being retried: the page
            # grows and its newest filename moves, without the test having to
            # win a race against the watcher's loop.
            stub.add(body="fresh traffic @agent-a")

        wait = float(query.get("wait") or 0)
        after = query.get("after") or None
        order = query.get("order") or "desc"
        limit = int(query.get("limit") or 0)

        t0 = time.monotonic()
        items, truncated = stub.select(after, order, limit)
        status = "delivered" if items else "timeout"
        if wait > 0 and not items:
            if stub.instant_status:
                # No hold at all: degraded/evicted, or a truthful zero-wait
                # timeout — waited_ms comes out ~0 either way.
                status = stub.instant_status
            else:
                deadline = t0 + wait
                while time.monotonic() < deadline:
                    time.sleep(0.02)
                    items, truncated = stub.select(after, order, limit)
                    if items:
                        status = "delivered"
                        break
        self._send(200, stub.page(items, truncated, wait,
                                  int((time.monotonic() - t0) * 1000), status))

    def _send(self, code: int, payload: bytes) -> None:
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client was killed mid-park; that is a tested scenario


@pytest.fixture
def stub():
    s = Stub()
    handler = type("_BoundHandler", (_Handler,), {"stub": s})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    s.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield s
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ── driving the script ────────────────────────────────────────────────


def script_env(state: Path | None, *, wait: str = "1", backoff: str = "0",
               **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    env["no_proxy"] = "*"
    env["NO_PROXY"] = "*"
    env["COLLAB_WATCH_WAIT"] = wait
    env["COLLAB_WATCH_BACKOFF"] = backoff
    if state is not None:
        env["COLLAB_WATCH_DIR"] = str(state)
    # Deliberately NOT setting LC_ALL: the script must impose its own.
    env.pop("LC_ALL", None)
    env.update(extra)
    return env


def argv(stub: Stub, handle: str = "agent-a", stream: str | None = None,
         *flags: str) -> list[str]:
    args = ["sh", SCRIPT, stub.base_url, handle]
    if stream:
        args.append(stream)
    return args + list(flags)


def run(stub: Stub, state: Path | None, *flags: str, handle: str = "agent-a",
        stream: str | None = None, timeout: float = 30,
        **env_kw: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv(stub, handle, stream, *flags),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=script_env(state, **env_kw), timeout=timeout,
    )


def popen(stub: Stub, state: Path | None, *flags: str, handle: str = "agent-a",
          stream: str | None = None, **env_kw: str) -> subprocess.Popen:
    return subprocess.Popen(
        argv(stub, handle, stream, *flags),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=script_env(state, **env_kw),
    )


def stop(proc: subprocess.Popen, timeout: float = 10) -> tuple[str, str]:
    """Terminate a watcher and collect its output. The script traps TERM and
    exits through its cleanup path, but a shell only runs the trap once the
    foreground curl returns — hence the tiny waits everywhere."""
    if proc.poll() is None:
        proc.terminate()
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate(timeout=timeout)


def wait_until(pred, timeout: float = 12.0, tick: float = 0.02) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(tick)
    return False


def wait_for_next_request(stub: Stub, timeout: float = 12.0) -> bool:
    """Wait until the watcher polls again. Used as a loop-progress barrier: the
    next request can only happen after everything the current pass still had to
    do (cursor write, heartbeat, log line) has finished, which makes assertions
    on that output deterministic instead of racing the terminate()."""
    seen = stub.n_requests()
    return wait_until(lambda: stub.n_requests() > seen, timeout=timeout)


def fresh(tmp_path: Path, cursor: str | None = None, stream: str = "updates") -> Path:
    """A state dir; with `cursor` given, one that skips cold-start baselining
    (pass "" for "nothing seen yet, deliver everything")."""
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if cursor is not None:
        (state / f"cursor.{stream}").write_text(cursor + "\n")
    return state


def heartbeat(state: Path) -> tuple[int, str]:
    epoch, status = (state / "heartbeat").read_text().split()[:2]
    return int(epoch), status


def journal(state: Path, name: str = "delivered.jsonl") -> list[dict]:
    path = state / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── 1. cold start: baseline, no history dump ──────────────────────────


def test_cold_start_baselines_newest_without_printing(stub, tmp_path):
    """A first run in a fresh state dir records the newest EXISTING filename as
    its baseline, prints nothing, and stays parked (§5.2 cold-start contract)."""
    stub.add(body="history one")
    newest = stub.add(body="history two")
    state = tmp_path / "state"

    proc = popen(stub, state)
    try:
        cursor = state / "cursor.updates"
        assert wait_until(cursor.exists), "no cursor file was written"
        assert cursor.read_text().strip() == newest
        assert wait_until(lambda: stub.n_requests() >= 2), "did not park after baselining"
        assert proc.poll() is None
    finally:
        out, err = stop(proc)

    assert out.strip() == "", "cold start must not dump history to stdout"
    assert "cold start" in err
    baseline_query = stub.requests[0][1]
    assert baseline_query["limit"] == "1"
    assert baseline_query["order"] == "desc"
    assert baseline_query["expand"] == "true"
    assert "wait" not in baseline_query, "the baseline request must not park"


def test_cold_start_on_empty_stream_writes_empty_cursor(stub, tmp_path):
    state = tmp_path / "state"
    proc = popen(stub, state)
    try:
        cursor = state / "cursor.updates"
        assert wait_until(cursor.exists)
        assert cursor.read_text().strip() == ""
    finally:
        stop(proc)


# ── 2. delivery ───────────────────────────────────────────────────────


def test_delivery_prints_page_advances_cursor_and_journals(stub, tmp_path):
    """The happy path: page on stdout, cursor := the response's own `cursor`
    field, page appended to delivered.jsonl, heartbeat says delivered, exit 0."""
    state = fresh(tmp_path, cursor="")
    filename = stub.add(body="ping @agent-a please look")

    result = run(stub, state)

    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    assert [item["filename"] for item in page["items"]] == [filename]
    assert page["cursor"] == filename
    assert (state / "cursor.updates").read_text().strip() == filename
    assert journal(state) == [page]
    assert heartbeat(state)[1] == "delivered"
    query = stub.last_query()
    assert query["order"] == "asc" and query["expand"] == "true"
    assert query["wait"] == "1" and query["limit"] == "10"
    assert "after" not in query, "an empty cursor must not be sent as after="


def test_collab_watch_state_overrides_only_the_cursor_path(stub, tmp_path):
    """eq2 compatibility: COLLAB_WATCH_STATE keeps pointing at a cursor FILE,
    while the heartbeat/lock/journal still live in the state directory."""
    state = fresh(tmp_path)
    cursor = tmp_path / "legacy.cursor"
    cursor.write_text("\n")
    filename = stub.add()

    result = run(stub, state, COLLAB_WATCH_STATE=str(cursor))

    assert result.returncode == 0, result.stderr
    assert cursor.read_text().strip() == filename
    assert not (state / "cursor.updates").exists()
    assert (state / "heartbeat").exists() and (state / "delivered.jsonl").exists()


def test_second_run_resumes_from_cursor_and_drains_forward(stub, tmp_path):
    """Consecutive runs page forward oldest-first with no gaps and no dups."""
    state = fresh(tmp_path, cursor="")
    landed = [stub.add(body=f"burst {i} @agent-a") for i in range(12)]

    first = run(stub, state)
    second = run(stub, state)

    assert (first.returncode, second.returncode) == (0, 0), first.stderr + second.stderr
    page1 = [i["filename"] for i in json.loads(first.stdout)["items"]]
    page2 = [i["filename"] for i in json.loads(second.stdout)["items"]]
    assert len(page1) == 10, "the served page limit is 10"
    assert page1 + page2 == landed
    assert (state / "cursor.updates").read_text().strip() == landed[-1]
    assert len(journal(state)) == 2
    assert stub.requests[-1][1]["after"] == landed[9]


def test_unread_is_len_items_not_matched(stub, tmp_path):
    """`matched` is not an unread count: a page whose `matched` says 0 is still
    delivered (the field that produced a false 'up to date' in the field)."""
    stub.matched_override = 0
    state = fresh(tmp_path, cursor="")
    filename = stub.add()

    result = run(stub, state)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["matched"] == 0
    assert (state / "cursor.updates").read_text().strip() == filename


def test_journal_is_written_before_stdout(stub, tmp_path):
    """delivered.jsonl is the recovery path for discarded stdout, so it must be
    complete BEFORE the print. Proven by never reading the stdout pipe: the
    script blocks writing a page larger than the pipe buffer while the journal
    already holds it in full."""
    state = fresh(tmp_path, cursor="")
    stub.add(body="x" * 400_000)

    proc = popen(stub, state)
    try:
        path = state / "delivered.jsonl"
        assert wait_until(lambda: path.exists() and path.stat().st_size > 400_000), \
            "journal was not written while stdout was still blocked"
        assert proc.poll() is None, "the page should still be stuck in the pipe"
        # Only now drain the pipe, so the print completes on its own and the
        # run still ends the normal way (exit 0, cursor advanced).
        out, err = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            stop(proc)
    assert proc.returncode == 0, err
    assert len(json.loads(out)["items"]) == 1
    assert (state / "cursor.updates").read_text().strip() != ""


# ── 3. cursor integrity (§5.5) ────────────────────────────────────────


def test_cursor_field_wins_over_poisoned_record_content(stub, tmp_path):
    """Record content is agent-authored and must never reach the cursor.

    Two independent attacks in one page: a `cursor`/`filename` FRONTMATTER key
    (a genuine nested JSON key — caught by only considering the tail after the
    items array's closing bracket) and a body that spells out
    `],"cursor":"<poison>"` (caught by JSON string escaping, which turns its
    quotes into \\" so the ten-byte key sequence cannot occur)."""
    state = fresh(tmp_path, cursor="")
    real = stub.add(
        body=f'see also ["{POISON}"] and ],"cursor":"{POISON}" plus "filename":"{POISON}"',
        frontmatter={"cursor": POISON, "filename": POISON, "next": POISON},
    )

    result = run(stub, state)

    assert result.returncode == 0, result.stderr
    raw = result.stdout
    assert POISON in raw, "the poison must really be in the page we parsed"
    assert raw.count(POISON) >= 3
    assert (state / "cursor.updates").read_text().strip() == real


def test_page_without_cursor_field_is_fatal(stub, tmp_path):
    """A server that does not implement §4.4 gets a loud exit 1, not a guessed
    cursor (guessing is the vulnerability) and not a silent spin."""
    stub.omit_cursor = True
    state = fresh(tmp_path, cursor="")
    stub.add()

    result = run(stub, state)

    assert result.returncode == 1
    assert "cursor" in result.stderr
    assert (state / "cursor.updates").read_text().strip() == ""
    assert heartbeat(state)[1] == "no_cursor"


# ── 4. idle behaviour: timeouts vs degradation ────────────────────────


def test_timeout_answers_keep_the_watcher_looping(stub, tmp_path):
    """An empty page with watch.status=timeout is the routine idle path: the
    watcher keeps polling, and rewrites the heartbeat on every pass.

    The pulse assertion needs a barrier, not a tolerance: `hb waiting` is
    stamped immediately before each poll, so once the stub has seen a FURTHER
    request the heartbeat of that later pass is already on disk — and its epoch
    must be strictly newer than the one captured before the barrier (passes are
    at least the idle floor apart, well over `date +%s` granularity). Comparing
    with >= instead would pass even against a watcher that never wrote again."""
    state = fresh(tmp_path, cursor="")

    proc = popen(stub, state)
    try:
        assert wait_until(lambda: stub.n_requests() >= 2, timeout=20), \
            f"only {stub.n_requests()} requests — the loop stalled"
        assert proc.poll() is None
        before_epoch, before_status = heartbeat(state)
        assert before_status in {"waiting", "timeout"}, before_status

        assert wait_for_next_request(stub, timeout=20), \
            f"only {stub.n_requests()} requests — the loop stalled"
        assert wait_until(lambda: heartbeat(state)[0] > before_epoch, timeout=20), \
            ("the heartbeat was not rewritten on the later pass: "
             f"{before_epoch} {before_status} -> {heartbeat(state)}")
        assert heartbeat(state)[1] in {"waiting", "timeout"}
        assert proc.poll() is None
    finally:
        out, _err = stop(proc)
    assert out.strip() == ""


def test_degraded_answers_do_not_hot_loop(stub, tmp_path):
    """Instantly-empty answers (server over its waiter cap) must be paced, or
    degradation would increase load instead of shedding it."""
    stub.instant_status = "degraded"
    state = fresh(tmp_path, cursor="")

    proc = popen(stub, state)
    try:
        # Measure the request RATE from the first poll, not from launch: process
        # startup under load would otherwise eat into the window and make the
        # lower bound a coin flip.
        assert wait_until(lambda: stub.n_requests() >= 1), "the watcher never polled"
        before = stub.n_requests()
        time.sleep(4.0)
        extra = stub.n_requests() - before
        assert proc.poll() is None
        assert 1 <= extra <= 3, f"{extra} further requests in 4s — pacing is broken"
    finally:
        _out, err = stop(proc)
    assert "degraded" in err


def test_instant_timeout_answers_do_not_hot_loop(stub, tmp_path):
    """A truthful `timeout` can still be instant, so the idle floor applies to it
    too. A self-hosted server with LONGPOLL_MAX_WAIT_S=0 answers
    status=timeout, waited_ms=0 immediately and honestly; a client that trusted
    the status instead of the clock would poll it flat out — degradation
    amplification against the server least able to take it (§3.2.1)."""
    stub.instant_status = "timeout"
    state = fresh(tmp_path, cursor="")

    proc = popen(stub, state)
    try:
        assert wait_until(lambda: stub.n_requests() >= 1), "the watcher never polled"
        before = stub.n_requests()
        time.sleep(4.0)
        extra = stub.n_requests() - before
        assert proc.poll() is None
        # IDLE_FLOOR_S is 2s, so 4s of wall clock allows 2 further polls (+1 for
        # the second boundary); hundreds means the floor was skipped entirely.
        assert 1 <= extra <= 3, f"{extra} further requests in 4s — pacing is broken"
        assert heartbeat(state)[1] in {"waiting", "timeout"}
    finally:
        _out, err = stop(proc)
    assert "watch.status=timeout" not in err, \
        "a plain timeout is the routine idle path — pace it, do not log it"


def test_instant_empty_without_watch_status_is_also_paced(stub, tmp_path):
    """Fallback for a server that sends no watch metadata: the <2s elapsed
    heuristic still stops the hot loop."""
    state = fresh(tmp_path, cursor="")
    proc = popen(stub, state, wait="0")  # wait=0 -> no `watch` block at all
    try:
        assert wait_until(lambda: stub.n_requests() >= 1), "the watcher never polled"
        before = stub.n_requests()
        time.sleep(4.0)
        extra = stub.n_requests() - before
        assert proc.poll() is None
        assert 1 <= extra <= 3, f"{extra} further requests in 4s — pacing is broken"
    finally:
        stop(proc)


# ── 5. errors: fast-fail vs backoff ───────────────────────────────────


def test_4xx_fails_fast_and_prints_the_error_body(stub, tmp_path):
    """A typo'd handle must die immediately with the server's own error body —
    in eq2 it retried for six minutes and died with an opaque curl rc=22."""
    stub.http_status = 404
    stub.error_body = json.dumps(
        {"error": {"code": "NOT_REGISTERED",
                   "message": "agent 'agent-typo' is not registered",
                   "hint": "register first via POST /v1/agents/register"}},
        separators=(",", ":"),
    ).encode()
    state = fresh(tmp_path, cursor="")

    result = run(stub, state, handle="agent-typo")

    assert result.returncode == 1
    assert "NOT_REGISTERED" in result.stderr
    assert "register first via POST /v1/agents/register" in result.stderr
    assert result.stdout == ""
    assert stub.n_requests() == 1, "4xx must not be retried"


def test_3xx_fails_fast_with_a_redirect_hint(stub, tmp_path):
    """A redirect is permanent, not transient. `http://<org>.hf.space` redirects
    to https and this client deliberately does not follow redirects (a redirect
    can move a watcher to another host, scheme or handle), so retrying can never
    succeed: it burned the whole backoff ladder and then reported an outage
    instead of the one-word fix."""
    stub.http_status = 301
    state = fresh(tmp_path, cursor="")
    t0 = time.monotonic()

    result = run(stub, state, timeout=60)
    elapsed = time.monotonic() - t0

    assert result.returncode == 1
    assert stub.n_requests() == 1, "a 3xx must not be retried"
    assert elapsed < 10, f"took {elapsed:.1f}s — that is the backoff ladder, not a fast fail"
    assert "301" in result.stderr
    assert "redirect" in result.stderr
    assert f"https://{stub.base_url.split('://', 1)[1]}" in result.stderr, \
        "an http:// base must be told to try https://"
    assert result.stdout == ""
    assert heartbeat(state)[1] == "http_301"


def test_ten_failures_give_up_with_exit_4_and_gave_up_heartbeat(stub, tmp_path):
    """5xx shares the backoff ladder; a streak of 10 exits 4 and leaves
    status=gave_up behind, so --status can report it once the process is gone."""
    stub.http_status = 503
    state = fresh(tmp_path, cursor="")

    result = run(stub, state, timeout=60)

    assert result.returncode == 4
    assert stub.n_requests() == 10
    assert "giving up after 10" in result.stderr
    assert heartbeat(state)[1] == "gave_up"


def test_backoff_ladder_is_used_when_not_disabled(stub, tmp_path):
    """Sanity check on the ladder itself (base 1 -> 1s, 2s, ... ): three
    failures cannot happen faster than the first two sleeps."""
    stub.http_status = 500
    state = fresh(tmp_path, cursor="")
    proc = popen(stub, state, backoff="1")
    try:
        assert wait_until(lambda: stub.n_requests() >= 3, timeout=15)
        assert stub.n_requests() <= 4, "backoff did not slow the retries down"
    finally:
        stop(proc)


def test_non_numeric_wait_is_rejected_at_startup(stub, tmp_path):
    """eq2 died with `unbound variable` on this; it is a config error, exit 1."""
    result = run(stub, fresh(tmp_path), wait="fifty-five")
    assert result.returncode == 1
    assert "COLLAB_WATCH_WAIT" in result.stderr
    assert stub.n_requests() == 0


# ── 6. --max-wait ─────────────────────────────────────────────────────


def test_max_wait_exits_3_on_a_clean_no_mail_timeout(stub, tmp_path):
    """Exit 3 exists so a bounded wait that found nothing is distinguishable
    from a watcher that was killed.

    The elapsed assertion is the contract, not a tolerance: N is a FLOOR, so
    exit 3 must never fire before it. `date +%s` truncates to whole seconds, so
    a deadline computed naively from it lands up to a second early — which this
    assertion caught intermittently before the script padded for it."""
    state = fresh(tmp_path, cursor="")
    t0 = time.monotonic()

    result = run(stub, state, "--max-wait", "2", timeout=60)
    elapsed = time.monotonic() - t0

    assert result.returncode == 3
    assert result.stdout.strip() == ""
    assert elapsed >= 2.0, f"gave up after {elapsed:.2f}s of a 2s floor"
    assert elapsed < 12, f"overshot the bound by too much ({elapsed:.2f}s)"
    assert "clean timeout" in result.stderr
    assert heartbeat(state)[1] == "no_mail"


def test_max_wait_still_delivers_when_mail_arrives(stub, tmp_path):
    state = fresh(tmp_path, cursor="")
    filename = stub.add()
    result = run(stub, state, "--max-wait", "10", timeout=30)
    assert result.returncode == 0, result.stderr
    assert [i["filename"] for i in json.loads(result.stdout)["items"]] == [filename]


# ── 7. --peek ─────────────────────────────────────────────────────────


def test_peek_prints_pending_without_advancing_the_cursor(stub, tmp_path):
    """--peek is read-only: exit 10, page on stdout, cursor untouched, and no
    journal or heartbeat side effects (a peek is not a delivery, and it must
    never make a dead watcher look alive)."""
    old = stub.add(body="already seen")
    state = fresh(tmp_path, cursor=old)
    stub.add(body="new one")
    stub.add(body="new two")

    result = run(stub, state, "--peek")

    assert result.returncode == 10
    assert len(json.loads(result.stdout)["items"]) == 2
    assert (state / "cursor.updates").read_text().strip() == old
    assert not (state / "delivered.jsonl").exists()
    assert not (state / "heartbeat").exists()
    query = stub.last_query()
    assert query["wait"] == "0" and query["after"] == old
    assert stub.n_requests() == 1


def test_peek_exits_0_when_caught_up(stub, tmp_path):
    newest = stub.add()
    state = fresh(tmp_path, cursor=newest)
    result = run(stub, state, "--peek")
    assert result.returncode == 0
    assert json.loads(result.stdout)["items"] == []


def test_peek_on_a_fresh_state_dir_baselines_instead_of_dumping_history(stub, tmp_path):
    """Without this, the first peek would print history the watcher will then
    skip — the cursor would jump straight to newest afterwards."""
    stub.add(body="history")
    newest = stub.add(body="history two")
    state = tmp_path / "state"

    result = run(stub, state, "--peek")

    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert (state / "cursor.updates").read_text().strip() == newest
    assert not (state / "heartbeat").exists()


# ── 8. --exec ack semantics (§5.4) ────────────────────────────────────


def _handler(tmp_path: Path, exit_code: int) -> tuple[str, Path]:
    """A handler script that logs the page it was handed, then exits `code`."""
    log = tmp_path / "handler.log"
    script = tmp_path / "handler.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'cat >>"{log}"\n'
        f'printf "\\n" >>"{log}"\n'
        f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return f"sh {script}", log


def test_exec_advances_the_cursor_only_after_the_handler_exits_0(stub, tmp_path):
    cmd, log = _handler(tmp_path, 0)
    state = fresh(tmp_path, cursor="")
    cursor = state / "cursor.updates"

    proc = popen(stub, state, "--exec", cmd)
    try:
        first = stub.add(body="one @agent-a")
        assert wait_until(lambda: cursor.read_text().strip() == first), \
            f"cursor never advanced (log={log.read_text() if log.exists() else '<none>'})"
        second = stub.add(body="two @agent-a")
        assert wait_until(lambda: cursor.read_text().strip() == second), \
            "the loop did not keep running after the first ack"
        assert proc.poll() is None, "--exec is a loop; it must not exit on delivery"
        assert wait_for_next_request(stub), "the watcher did not park again"
    finally:
        _out, err = stop(proc)

    handled = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert [len(p["items"]) for p in handled] == [1, 1]
    assert "handler acked" in err
    assert len(journal(state)) == 2


def test_exec_failure_holds_the_cursor_then_dead_letters(stub, tmp_path):
    """A permanently failing handler must not deafen the agent: the page is
    retried COLLAB_WATCH_EXEC_RETRIES times, then dead-lettered and skipped."""
    cmd, log = _handler(tmp_path, 3)
    state = fresh(tmp_path, cursor="")
    cursor = state / "cursor.updates"

    proc = popen(stub, state, "--exec", cmd, COLLAB_WATCH_EXEC_RETRIES="3")
    try:
        filename = stub.add(body="poison @agent-a")
        dead = state / "dead-letter.jsonl"
        assert wait_until(dead.exists, timeout=20), "page was never dead-lettered"
        # The dead-letter append lands just before the cursor write, so wait for
        # it rather than racing the two.
        assert wait_until(lambda: cursor.read_text().strip() == filename), \
            "cursor must advance past a dead-lettered page"
        assert proc.poll() is None
        assert wait_for_next_request(stub), \
            "the loop did not resume after skipping the poison page"
    finally:
        _out, err = stop(proc)

    attempts = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(attempts) == 3, f"expected 3 handler attempts, got {len(attempts)}"
    assert len(journal(state, "dead-letter.jsonl")) == 1
    assert len(journal(state)) == 3, "every delivery attempt is journaled"
    assert "NOT advancing the cursor" in err
    assert "dead-lettered" in err
    # The heartbeat holds the LATEST loop status, so by now it has moved on to
    # the next park — the dead-letter is recorded in the file, not the pulse.


def test_exec_dead_letters_even_when_the_page_keeps_growing(stub, tmp_path):
    """The poison guard must not be defeatable by ordinary traffic.

    The retry counter is keyed on the page's START (the parked cursor), because
    that is the one thing a failing handler cannot move — it holds the cursor by
    design. Keyed instead on the page's newest filename, every message that
    landed during the retries changed the identity, reset the counter, and the
    dead-letter never fired: a permanently failing handler kept the agent deaf
    for as long as the board stayed busy. (It only recovered once the backlog
    exceeded the page limit and pinned the newest filename — i.e. never, on a
    quiet-but-not-silent board.)

    Here the stub lands a fresh message on each of the first two polls, so the
    page grows and its newest filename moves between every retry."""
    cmd, log = _handler(tmp_path, 1)
    state = fresh(tmp_path, cursor="")
    cursor = state / "cursor.updates"
    stub.add(body="poison @agent-a")
    stub.grow_polls = 2

    proc = popen(stub, state, "--exec", cmd, COLLAB_WATCH_EXEC_RETRIES="2")
    try:
        dead = state / "dead-letter.jsonl"
        assert wait_until(dead.exists, timeout=20), \
            "the growing page was never dead-lettered — the retry counter is " \
            "being reset by new arrivals, so the handler can deafen the agent"
        assert wait_until(lambda: cursor.read_text().strip() != "")
        assert wait_for_next_request(stub), "the loop did not resume"
    finally:
        _out, err = stop(proc)

    attempts = [line for line in log.read_text().splitlines() if line.strip()]
    assert len(attempts) == 2, \
        f"expected exactly COLLAB_WATCH_EXEC_RETRIES=2 attempts, got {len(attempts)}"

    delivered = journal(state)
    assert len(delivered) == 2
    assert len(delivered[1]["items"]) > len(delivered[0]["items"]), \
        "the page did not actually grow between retries — test is not adversarial"

    dead_page = journal(state, "dead-letter.jsonl")
    assert len(dead_page) == 1
    assert len(dead_page[0]["items"]) >= 2, "the whole delivered page is dead-lettered"
    # The cursor skips to the end of the page that was actually dead-lettered,
    # so nothing is dropped beyond what the journal now holds.
    assert cursor.read_text().strip() == dead_page[0]["cursor"]
    assert "dead-lettered" in err


# ── 9. the lock (§5.2) ────────────────────────────────────────────────


def test_second_watcher_exits_5_naming_the_live_pid(stub, tmp_path):
    """Two watchers sharing a cursor file double-deliver and roll the cursor
    back; the second instance refuses to start."""
    state = fresh(tmp_path, cursor="")
    first = popen(stub, state)
    try:
        pid_file = state / "lock" / "pid"
        assert wait_until(pid_file.exists), "no lock was taken"
        assert pid_file.read_text().strip() == str(first.pid)

        second = run(stub, state, timeout=20)
        assert second.returncode == 5
        assert str(first.pid) in second.stderr
        assert second.stdout == ""
    finally:
        stop(first)
    assert wait_until(lambda: not (state / "lock").exists()), \
        "the lock must be released on exit"


def test_stale_lock_is_reclaimed(stub, tmp_path):
    """A lock whose PID is gone (kill -9, harness reaping) is not a wall."""
    reaped = subprocess.Popen(["true"])
    reaped.wait()
    state = fresh(tmp_path, cursor="")
    (state / "lock").mkdir()
    (state / "lock" / "pid").write_text(f"{reaped.pid}\n")
    filename = stub.add()

    result = run(stub, state)

    assert result.returncode == 0, result.stderr
    assert "stale lock" in result.stderr
    assert (state / "cursor.updates").read_text().strip() == filename


def test_the_lock_is_per_handle_and_status_is_stream_aware(stub, tmp_path):
    """The lock and heartbeat are per-HANDLE while cursors are per-stream: one
    watcher per agent is the whole point of the unified `updates` stream.

    So a second watcher is refused even on another stream (exit 5, naming the
    live PID) — and the flip side must hold too: that watcher's pulse is NOT
    liveness for the stream it is not watching, or `--status <base> <me> feed`
    would report a healthy watcher while nothing advances cursor.feed."""
    state = fresh(tmp_path, cursor="", stream="updates")
    (state / "cursor.feed").write_text("\n")  # so --status feed does its request

    watcher = popen(stub, state)
    try:
        assert wait_until((state / "lock" / "pid").exists), "no lock was taken"
        assert wait_until((state / "heartbeat").exists)
        assert wait_until(lambda: stub.n_requests() >= 1)

        other_stream = run(stub, state, stream="feed", timeout=20)
        assert other_stream.returncode == 5, other_stream.stderr
        assert str(watcher.pid) in other_stream.stderr
        assert "handle" in other_stream.stderr, "the message must say per-handle"
        assert other_stream.stdout == ""

        behind_on_feed = run(stub, state, "--status", stream="feed", timeout=20)
        assert behind_on_feed.returncode == 11, behind_on_feed.stdout
        fields = _status_line(behind_on_feed)
        assert fields["STATUS"] == "NO_WATCHER"
        assert fields["STREAM"] == "updates", "the live watcher's stream is reported"
        assert fields["PID"] == str(watcher.pid)
        assert "updates" in behind_on_feed.stderr, \
            "the caller must be told a watcher is alive on the other stream"

        on_updates = run(stub, state, "--status", stream="updates", timeout=20)
        assert on_updates.returncode == 0, on_updates.stdout + on_updates.stderr
        assert _status_line(on_updates)["STATUS"] == "OK"
        assert _status_line(on_updates)["STREAM"] == "updates"
    finally:
        stop(watcher)


def test_status_10_outranks_a_watcher_on_another_stream(stub, tmp_path):
    """BEHIND still outranks every liveness verdict, including the new
    wrong-stream one: pending items are the actionable fact."""
    seen = stub.add()
    state = fresh(tmp_path, cursor=seen, stream="feed")
    stub.add()
    alive = subprocess.Popen(["sleep", "30"])
    try:
        (state / "lock").mkdir()
        (state / "lock" / "pid").write_text(f"{alive.pid}\n")
        (state / "heartbeat").write_text(f"{int(time.time())} waiting {alive.pid} updates\n")

        result = run(stub, state, "--status", stream="feed", timeout=20)

        assert result.returncode == 10, result.stdout + result.stderr
        fields = _status_line(result)
        assert fields["STATUS"] == "BEHIND" and fields["UNREAD"] == "1"
        assert fields["STREAM"] == "updates"
    finally:
        alive.terminate()
        alive.wait()


def test_an_empty_pid_file_is_not_stolen_from_a_live_acquirer(stub, tmp_path):
    """`mkdir` and the pid write cannot be one atomic step, so an empty lock/pid
    is usually a lock acquired microseconds ago. Reading it once and declaring
    the lock stale is how two watchers end up sharing one cursor file — the eq2
    failure the lock exists to prevent — so the pid is re-read after a second of
    grace, which a live acquirer wins."""
    state = fresh(tmp_path, cursor="")
    (state / "lock").mkdir()
    pid_file = state / "lock" / "pid"
    pid_file.write_text("")  # an acquirer between its mkdir and its pid write
    alive = subprocess.Popen(["sleep", "30"])
    watcher = popen(stub, state)
    try:
        time.sleep(0.3)  # inside the grace window, where the real race lands
        pid_file.write_text(f"{alive.pid}\n")
        # It must exit 5 on its own — no terminate(), or the signal would be
        # indistinguishable from the refusal under test.
        _out, err = watcher.communicate(timeout=20)
        assert watcher.returncode == 5, err
        assert str(alive.pid) in err
        assert pid_file.read_text().strip() == str(alive.pid), \
            "the acquirer's lock must survive"
    finally:
        if watcher.poll() is None:
            stop(watcher)
        alive.terminate()
        alive.wait()


def test_a_permanently_empty_pid_file_is_still_reclaimed(stub, tmp_path):
    """The other half of the grace: a crash between mkdir and the pid write
    leaves lock/pid empty forever, and that lock must not become a wall."""
    state = fresh(tmp_path, cursor="")
    (state / "lock").mkdir()
    (state / "lock" / "pid").write_text("")
    filename = stub.add()

    result = run(stub, state, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "stale lock" in result.stderr
    assert (state / "cursor.updates").read_text().strip() == filename


def test_exit_does_not_remove_a_lock_that_is_no_longer_ours(stub, tmp_path):
    """Whoever loses an acquisition race must not tear the winner's lock down on
    its way out: cleanup releases the lock only while lock/pid still holds $$."""
    state = fresh(tmp_path, cursor="")
    other = subprocess.Popen(["sleep", "30"])
    watcher = popen(stub, state)
    try:
        pid_file = state / "lock" / "pid"
        assert wait_until(lambda: pid_file.exists()
                          and pid_file.read_text().strip() == str(watcher.pid)), \
            "the watcher never took the lock"
        pid_file.write_text(f"{other.pid}\n")  # another watcher now owns it

        stop(watcher, timeout=20)

        assert (state / "lock").exists(), "the other watcher's lock was removed"
        assert pid_file.read_text().strip() == str(other.pid)
    finally:
        if watcher.poll() is None:
            stop(watcher)
        other.terminate()
        other.wait()


def test_peek_and_status_ignore_the_lock(stub, tmp_path):
    """Peeking/statusing beside a running watcher is the whole point of them."""
    state = fresh(tmp_path, cursor="")
    watcher = popen(stub, state)
    try:
        assert wait_until((state / "lock" / "pid").exists)
        assert run(stub, state, "--peek", timeout=20).returncode == 0
        assert run(stub, state, "--status", timeout=20).returncode == 0
    finally:
        stop(watcher)


# ── 10. --status (§6.1) ───────────────────────────────────────────────


def _status_line(result: subprocess.CompletedProcess) -> dict[str, str]:
    assert result.stdout.count("\n") == 1, f"--status prints ONE line: {result.stdout!r}"
    return dict(part.split("=", 1) for part in result.stdout.split())


def test_status_11_when_no_watcher_has_ever_run(stub, tmp_path):
    result = run(stub, tmp_path / "state", "--status")
    assert result.returncode == 11
    fields = _status_line(result)
    assert fields["STATUS"] == "NO_WATCHER"
    assert fields["UNREAD"] == "?"
    assert fields["HEARTBEAT_AGE"] == "-"
    assert fields["PID"] == "-"
    assert fields["STREAM"] == "-", "no heartbeat -> no watched stream to report"
    assert stub.n_requests() == 0, "no cursor -> nothing to compare -> no request"


def test_status_0_when_live_and_caught_up(stub, tmp_path):
    newest = stub.add()
    state = fresh(tmp_path, cursor=newest)
    alive = subprocess.Popen(["sleep", "30"])
    try:
        (state / "lock").mkdir()
        (state / "lock" / "pid").write_text(f"{alive.pid}\n")
        (state / "heartbeat").write_text(f"{int(time.time())} waiting {alive.pid} updates\n")

        result = run(stub, state, "--status")

        assert result.returncode == 0, result.stdout + result.stderr
        fields = _status_line(result)
        assert fields["STATUS"] == "OK"
        assert fields["UNREAD"] == "0"
        assert fields["PID"] == str(alive.pid)
        assert fields["STREAM"] == "updates"
        assert fields["LAST"] == "waiting"
        assert fields["HEARTBEAT_AGE"].endswith("s")
    finally:
        alive.terminate()
        alive.wait()


def test_status_10_when_behind_outranks_liveness(stub, tmp_path):
    """The §6 example line: BEHIND with no live PID and an ancient heartbeat.
    Being behind is the actionable verdict, so it wins."""
    seen = stub.add()
    state = fresh(tmp_path, cursor=seen)
    stub.add()
    stub.add()
    stub.add()
    (state / "heartbeat").write_text(f"{int(time.time()) - 412} gave_up 999999 updates\n")

    result = run(stub, state, "--status")

    assert result.returncode == 10
    fields = _status_line(result)
    assert fields["STATUS"] == "BEHIND"
    assert fields["UNREAD"] == "3"
    assert fields["PID"] == "-"
    assert fields["LAST"] == "gave_up", "the give-up must survive the process"
    assert int(fields["HEARTBEAT_AGE"].rstrip("s")) >= 412


def test_status_12_when_the_lock_is_live_but_the_heartbeat_is_stale(stub, tmp_path):
    newest = stub.add()
    state = fresh(tmp_path, cursor=newest)
    alive = subprocess.Popen(["sleep", "30"])
    try:
        (state / "lock").mkdir()
        (state / "lock" / "pid").write_text(f"{alive.pid}\n")
        (state / "heartbeat").write_text(f"{int(time.time()) - 3600} waiting {alive.pid} updates\n")

        result = run(stub, state, "--status")

        assert result.returncode == 12
        assert _status_line(result)["STATUS"] == "STALE"
    finally:
        alive.terminate()
        alive.wait()


def test_status_4_when_the_server_is_unreachable(stub, tmp_path):
    stub.http_status = 500
    state = fresh(tmp_path, cursor="20260728-120000-000_agent-b.md")
    result = run(stub, state, "--status", timeout=40)
    assert result.returncode == 4
    assert _status_line(result)["STATUS"] == "OFFLINE"
    assert _status_line(result)["UNREAD"] == "?"


def test_status_never_stamps_the_heartbeat(stub, tmp_path):
    """If --status wrote the heartbeat, checking on a dead watcher would keep
    reporting it as fresh forever."""
    state = fresh(tmp_path, cursor="")
    run(stub, state, "--status")
    assert not (state / "heartbeat").exists()


# ── 11. streams, state layout, locale, usage ──────────────────────────


@pytest.mark.parametrize(
    "stream,path,carries_as",
    [
        (None, "/v1/updates", True),
        ("updates", "/v1/updates", True),
        ("inbox", "/v1/inbox/agent-a", False),
        ("feed", "/v1/channels/feed", True),
    ],
)
def test_stream_selects_the_documented_endpoint(stub, tmp_path, stream, path, carries_as):
    stub.add()
    result = run(stub, tmp_path / "state", "--peek", stream=stream)
    assert result.returncode == 0, result.stderr
    assert stub.paths() == [path]
    query = stub.last_query()
    assert query.get("as") == ("agent-a" if carries_as else None)
    assert query["expand"] == "true"


def test_cursor_file_is_per_stream(stub, tmp_path):
    state = fresh(tmp_path)
    stub.add()
    run(stub, state, "--peek", stream="inbox")
    run(stub, state, "--peek", stream="feed")
    assert (state / "cursor.inbox").exists()
    assert (state / "cursor.feed").exists()
    assert not (state / "cursor.updates").exists()


def test_default_state_dir_is_per_host_and_handle(stub, tmp_path):
    """eq2's CWD-relative default silently re-baselined (and skipped mail) when
    a watcher was started from another directory."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home)}
    stub.add()
    result = subprocess.run(
        argv(stub, "agent-a", None, "--peek"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=script_env(None, **env), timeout=30, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    host = stub.base_url.split("://", 1)[1].replace(":", "_")
    assert (home / ".collab-watch" / host / "agent-a" / "cursor.updates").exists()


def test_behaviour_is_locale_independent(stub, tmp_path):
    """The script sets LC_ALL=C itself; a hostile inherited locale must not
    change filename comparisons or the ASCII character classes."""
    assert "LC_ALL=C" in Path(SCRIPT).read_text()
    state = fresh(tmp_path, cursor="")
    filename = stub.add()

    result = run(stub, state, LC_ALL="tr_TR.UTF-8", LANG="tr_TR.UTF-8",
                 LC_COLLATE="tr_TR.UTF-8", LC_CTYPE="tr_TR.UTF-8")

    assert result.returncode == 0, result.stderr
    assert (state / "cursor.updates").read_text().strip() == filename


@pytest.mark.parametrize(
    "args,needle",
    [
        ([], "missing <base-url>"),
        (["http://127.0.0.1:1"], "missing <handle>"),
        (["http://127.0.0.1:1", "agent-a", "bogus"], "stream must be"),
        (["http://127.0.0.1:1", "agent-a", "--nope"], "unknown flag"),
        (["http://127.0.0.1:1", "agent-a", "--peek", "--status"], "mutually exclusive"),
        (["http://127.0.0.1:1", "agent-a", "--max-wait", "soon"], "--max-wait must be"),
        (["http://127.0.0.1:1", "agent-a", "--max-wait"], "--max-wait needs"),
        (["http://127.0.0.1:1", "agent-a", "--exec"], "--exec needs"),
        (["http://127.0.0.1:1", "agent-a", "--peek", "--max-wait", "5"], "wait mode only"),
        (["http://127.0.0.1:1", "agent-a", "extra", "updates", "x"], "unexpected argument"),
        (["ftp://nope", "agent-a"], "must start with http"),
        (["http://127.0.0.1:1", "../etc/passwd"], "characters outside"),
    ],
)
def test_usage_errors(tmp_path, args, needle):
    result = subprocess.run(
        ["sh", SCRIPT, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=script_env(tmp_path / "state"), timeout=20,
    )
    assert result.returncode in (1, 2), result.stderr
    assert needle in result.stderr
    if needle in {"must start with http", "characters outside"}:
        assert result.returncode == 1 or "usage:" in result.stderr
    else:
        assert result.returncode == 2


def test_help_exits_0_on_stdout():
    result = subprocess.run(["sh", SCRIPT, "--help"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=20)
    assert result.returncode == 0
    assert "exit codes:" in result.stdout
    for flag in ("--max-wait", "--exec", "--peek", "--status"):
        assert flag in result.stdout
    for warning in ("do NOT wrap", "do NOT detach", "AT-LEAST-ONCE"):
        assert warning in result.stdout, f"the header must keep documenting: {warning}"


# ── 12. the script itself ─────────────────────────────────────────────


def test_script_is_posix_sh_clean():
    assert subprocess.run(["sh", "-n", SCRIPT], timeout=30).returncode == 0
    assert "#!/bin/sh" in Path(SCRIPT).read_text().splitlines()[0]


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_is_clean():
    result = subprocess.run(
        ["shellcheck", "--shell=sh", "--severity=warning", SCRIPT],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout


def test_no_bashisms():
    """POSIX sh only: the script is served to agents whose /bin/sh may be dash,
    busybox ash or ksh."""
    code = "\n".join(
        line for line in Path(SCRIPT).read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for bashism in ("[[", "function ", " == ", "local ", "echo -e", "&>", "$RANDOM"):
        assert bashism not in code, f"bashism in the script: {bashism!r}"
