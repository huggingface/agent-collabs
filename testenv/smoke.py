#!/usr/bin/env python3
"""End-to-end smoke of the channels + watch features against a running testenv
(./testenv/up.sh). Stdlib only — no deps, no venv:

    ./testenv/up.sh --reset && python3 testenv/smoke.py

These checks are NOT idempotent and require a FRESH bucket: they assert on
first-creation (`created: True`), on auto-subscribe, on the default quiet notify
level, and on exact content promotion. Re-running against a stack that has
already been smoked fails in confusing ways (`created: False`,
`ALREADY_PROMOTED`, a channel already flipped to `notify: all`) — that is dirty
state, not a regression. Always `--reset` first.

Exercises the backend as agents (raw + bucket-source + subscribe proofs), the
dashboard as a fake-logged-in human (proxies, composer channel post, creation
endpoint), and the watch/long-poll surface (parked `wait=` polls, per-channel
notify levels, the digest's watch blocks, and the served collab_watch.sh client
driven as a real agent would). Prints one line per check; exits non-zero on any
failure.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8100"
DASH = "http://127.0.0.1:7861"
ROOT = Path(__file__).resolve().parent.parent
BUCKETS = ROOT / ".testenv" / "buckets"
ORG, SLUG = "local-org", "collab"
WATCH_SH = ROOT / "backend" / "clients" / "collab_watch.sh"

# Long-poll timings. Everything is kept short deliberately: this is a pre-merge
# check, not a soak test, so no park here uses the 55s production default.
PARK_SETTLE_S = 0.8   # time given to a background poll to reach the registry
EARLY_S = 5.0         # a woken park must return well inside its wait budget

_jar = http.cookiejar.CookieJar()
_dash_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))

failures: list[str] = []
_n = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _n
    _n += 1
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(f"{label}: {detail}")


def req(url: str, payload: dict | None = None, bearer: str | None = None,
        opener=None, method: str | None = None) -> tuple[int, dict | list | str]:
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if data is not None:
        r.add_header("content-type", "application/json")
    if bearer:
        r.add_header("authorization", f"Bearer {bearer}")
    op = opener or urllib.request.urlopen
    try:
        resp = op(r) if opener else urllib.request.urlopen(r)
        body = resp.read().decode()
        code = resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        code = e.code
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        return code, body


def get(url: str, timeout: float = 90) -> tuple[int, dict | list | str]:
    """GET with an explicit socket timeout. Every `wait=` call goes through
    here: a parked poll must never hang the whole smoke run if the server
    forgets to answer it."""
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url), timeout=timeout)
        code, body = resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read().decode()
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        return code, body


def raw_get(url: str) -> tuple[int, str, str]:
    """(status, body text, content-type) — for the non-JSON /v1/watch.sh."""
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url), timeout=30)
        return resp.status, resp.read().decode(), resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), e.headers.get("content-type", "")


def stream_url(stream: str, handle: str, **params) -> str:
    """A watchable stream URL with the list grammar spelled out as query args."""
    q = {k: v for k, v in params.items() if v is not None}
    if stream == "inbox":
        path = f"/v1/inbox/{handle}"
    else:
        q["as"] = handle
        path = "/v1/updates" if stream == "updates" else "/v1/channels/feed"
    return f"{API}{path}?{urllib.parse.urlencode(q)}"


class Park(threading.Thread):
    """A `wait=` poll held open in a background thread, so the main thread can
    post the message that is supposed to resolve it. Records how long the
    server took to answer — the whole point of the feature is that a delivery
    returns early instead of burning the wait budget."""

    def __init__(self, url: str):
        super().__init__(daemon=True)
        self.url = url
        self.code = 0
        self.doc: dict = {}
        self.elapsed = -1.0

    def run(self) -> None:
        t0 = time.time()
        code, doc = get(self.url)
        self.elapsed = time.time() - t0
        self.code = code
        self.doc = doc if isinstance(doc, dict) else {"raw": doc}

    def settle(self) -> None:
        self.start()
        time.sleep(PARK_SETTLE_S)

    def finish(self, timeout: float = 40) -> tuple[list, dict, str]:
        """(items, watch block, watch.status) once the poll has returned."""
        self.join(timeout)
        items = self.doc.get("items") or []
        watch = self.doc.get("watch") or {}
        return items, watch, watch.get("status", "")


def watch_run(state: Path, *args: str, wait: str = "2", timeout: float = 60):
    """One `sh collab_watch.sh <base> <handle> ...` run, pinned to its own state
    directory so runs are isolated and nothing is left in $HOME."""
    env = dict(os.environ, COLLAB_WATCH_DIR=str(state), COLLAB_WATCH_WAIT=wait)
    return subprocess.run(
        ["sh", str(WATCH_SH), API, *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )


def watch_bg(state: Path, *args: str, wait: str = "2") -> subprocess.Popen:
    """The same, left running: the client is single-shot exit-on-mail, so the
    only way to observe a live watcher (lock held, heartbeat fresh) is to have
    one parked while we look at it."""
    env = dict(os.environ, COLLAB_WATCH_DIR=str(state), COLLAB_WATCH_WAIT=wait)
    return subprocess.Popen(
        ["sh", str(WATCH_SH), API, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def backend_agent_flows() -> None:
    print("backend — agent flows")

    code, doc = req(f"{API}/v1")
    paths = {e["path"] for e in doc.get("endpoints", [])}
    check("discovery lists channel endpoints",
          {"/v1/channels", "/v1/channels/feed"} <= paths and "channels" in doc.get("conventions", {}))

    # create (organizer human + Bearer) → auto-announce on the board
    code, ch = req(f"{API}/v1/channels", {
        "name": "eval-harness", "agent_id": "human-tester",
        "body": "Scoring, verification, and how to not fool ourselves. Bring measurements.",
    }, bearer="any-token")
    check("organizer creates channel (201 + announcement)",
          code == 201 and ch.get("created") is True and ch.get("announcement"), str(ch))
    ann = ch.get("announcement")

    code, board = req(f"{API}/v1/messages?expand=true&limit=50")
    files = {m["filename"] for m in board["items"]}
    check("announcement is a board message", ann in files)

    code, rej = req(f"{API}/v1/channels", {"name": "agent-room", "agent_id": "delta-coder", "body": "mine"})
    check("agent create rejected (403 NOT_ORGANIZER)",
          code == 403 and rej["error"]["code"] == "NOT_ORGANIZER", str(rej))
    code, _ = req(f"{API}/v1/channels", {"name": "feed", "agent_id": "human-tester", "body": "x"},
                  bearer="any-token")
    check("reserved name 'feed' → 400", code == 400)
    code, _ = req(f"{API}/v1/channels", {"name": "empty", "agent_id": "human-tester", "body": "  "},
                  bearer="any-token")
    check("empty theme → 400", code == 400)

    board_count = board["count"]

    # channel post (raw) with a mention → fan-out + auto-subscribe
    code, msg = req(f"{API}/v1/messages", {
        "agent_id": "delta-coder", "channel": "eval-harness",
        "body": "@byte-bandit the scorer truncates at 2^20 bytes — can you reproduce?",
    })
    check("channel post (raw): 201, channel stamped, auto-subscribed",
          code == 201 and msg.get("channel") == "eval-harness" and msg.get("auto_subscribed") is True
          and msg.get("mentions_delivered") == ["byte-bandit"], str(msg))

    code, inbox = req(f"{API}/v1/inbox/byte-bandit?expand=true")
    hits = [m for m in inbox["items"] if "truncates" in m["body"]]
    check("mention fan-out reached inbox, copy carries channel:",
          len(hits) == 1 and hits[0]["frontmatter"].get("channel") == "eval-harness")

    # bucket-source channel post: file dropped on disk AFTER boot
    scratch = BUCKETS / ORG / f"{SLUG}-byte-bandit" / "drafts"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "finding.md").write_text("---\ntype: note\n---\nlong-form finding from disk\n")
    code, msg = req(f"{API}/v1/messages", {
        "source": f"hf://buckets/{ORG}/{SLUG}-byte-bandit/drafts/finding.md",
        "channel": "eval-harness",
    })
    check("channel post (bucket source, live disk pickup): 201 via bucket",
          code == 201 and msg.get("via") == "bucket" and msg.get("channel") == "eval-harness", str(msg))

    code, board2 = req(f"{API}/v1/messages")
    check("board stays clean (channel posts don't land on it)", board2["count"] == board_count)

    code, chmsgs = req(f"{API}/v1/channels/eval-harness/messages?expand=true")
    check("channel messages listing", chmsgs["matched"] == 2
          and all(m["frontmatter"].get("channel") == "eval-harness" for m in chmsgs["items"]))

    # subscribe / feed / unsubscribe (source proof)
    src = {"source": f"hf://buckets/{ORG}/{SLUG}-delta-coder/subscribe.md"}
    code, feed = req(f"{API}/v1/channels/feed?as=delta-coder&expand=true")
    check("feed shows subscribed channel (auto-sub from posting)", feed["matched"] == 2)

    code, unsub = req(f"{API}/v1/channels/eval-harness/unsubscribe", src)
    check("unsubscribe: 200, changed", code == 200 and unsub.get("changed") is True, str(unsub))
    code, feed = req(f"{API}/v1/channels/feed?as=delta-coder")
    check("feed empty after unsubscribe", feed["matched"] == 0)
    code, unsub2 = req(f"{API}/v1/channels/eval-harness/unsubscribe", src)
    check("unsubscribe again: idempotent no-op", unsub2.get("changed") is False)
    code, sub = req(f"{API}/v1/channels/eval-harness/subscribe", src)
    check("re-subscribe via source proof", code == 200 and sub.get("changed") is True)
    code, spoof = req(f"{API}/v1/channels/eval-harness/subscribe", {"agent_id": "byte-bandit"})
    check("bare agent_id subscribe rejected (401)", code == 401)

    # feed cursor
    code, page1 = req(f"{API}/v1/channels/feed?as=delta-coder&order=asc&limit=1&expand=true")
    nxt = page1.get("next")
    code, page2 = req(f"{API}/v1/channels/feed?as=delta-coder&order=asc&after={nxt}&expand=true")
    names = [m["filename"] for m in page2["items"]]
    check("feed filename cursor pages correctly",
          len(page1["items"]) == 1 and len(names) == 1 and names[0] != page1["items"][0]["filename"])

    code, digest = req(f"{API}/v1/digest?as=delta-coder")
    chd = digest.get("channels", {})
    subd = chd.get("subscribed") or []
    check("digest channels block (summaries + subscribed activity)",
          chd.get("count") == 1 and [c["name"] for c in subd] == ["eval-harness"]
          and subd[0]["new_count"] == 2 and subd[0]["recent"], str(chd)[:200])

    code, bad = req(f"{API}/v1/messages", {
        "agent_id": "delta-coder", "body": "x", "channel": "eval-harness", "broadcast": True,
    })
    check("channel+broadcast rejected (422)", code == 422)

    code, human = req(f"{API}/v1/channels", {
        "name": "org-notes", "agent_id": "human-tester", "body": "Organizer planning notes.",
    }, bearer="any-token")
    check("second channel created (via dashboard path)",
          code == 201 and human.get("via") == "dashboard", str(human))

    # creator retry of the same create is idempotent (no error, no re-announce)
    payload = {"name": "org-notes", "agent_id": "human-tester",
               "body": "Organizer planning notes."}
    c2, r2 = req(f"{API}/v1/channels", payload, bearer="any-token")
    check("create retry is idempotent (200, no re-announce)",
          c2 == 200 and r2.get("created") is False
          and r2.get("announcement") is None, f"{c2} {r2}")


def dashboard_flows() -> None:
    print("dashboard — human flows (fake login)")

    code, chans = req(f"{DASH}/api/channels")
    names = {c["name"] for c in chans.get("items", [])}
    check("GET /api/channels proxies summaries", {"eval-harness", "org-notes"} <= names, str(names))

    code, detail = req(f"{DASH}/api/channels/eval-harness")
    handles = {m["handle"] for m in detail.get("members", [])}
    check("GET /api/channels/{name} detail (roster)", {"byte-bandit", "delta-coder"} <= handles, str(handles))

    code, msgs = req(f"{DASH}/api/channels/eval-harness/messages?expand=true&limit=200&order=desc")
    check("GET /api/channels/{name}/messages", len(msgs.get("items", [])) == 2)

    # fake login → session cookie → /api/me
    code, _ = req(f"{DASH}/login", opener=_dash_opener.open)
    code, me = req(f"{DASH}/api/me", opener=_dash_opener.open)
    check("dev login mints session; /api/me organizer hint",
          me.get("logged_in") is True and me.get("user") == "tester" and me.get("is_organizer") is True, str(me))

    # composer channel post through the dashboard (no fallback path)
    code, posted = req(f"{DASH}/api/messages",
                       {"body": "pinning this: rescoring tonight", "refs": [], "channel": "eval-harness"},
                       opener=_dash_opener.open)
    check("dashboard composer posts to channel via backend",
          code == 200 and posted.get("channel") == "eval-harness"
          and "channel: eval-harness" in posted["item"]["content"], str(posted)[:200])
    code, msgs = req(f"{API}/v1/channels/eval-harness/messages?agent=human-tester&expand=true")
    check("channel post landed as human-tester", msgs["matched"] == 1
          and msgs["items"][0]["frontmatter"]["via"] == "dashboard")

    # creation through the dashboard endpoint (the modal's path)
    code, created = req(f"{DASH}/api/channels",
                        {"name": "context-strategies", "body": "How to spend a context window. Measurements, not vibes."},
                        opener=_dash_opener.open)
    check("dashboard creates channel (announce + subscribe)",
          code == 200 and created.get("created") is True and created.get("announcement"), str(created)[:200])
    code, board = req(f"{DASH}/api/messages")
    anns = [m for m in board["items"] if created.get("announcement", "??") == m["filename"]]
    check("creation announcement visible on dashboard board", len(anns) == 1)

    # Same-creator re-POST is the theme-update path (200, created: false)…
    code, upd = req(f"{DASH}/api/channels", {"name": "context-strategies", "body": "Sharper scope."},
                    opener=_dash_opener.open)
    check("dashboard re-POST by creator updates theme (no re-announce)",
          code == 200 and upd.get("created") is False and upd.get("announcement") is None, str(upd))
    # …while a backend rejection surfaces verbatim (reserved name passes the
    # dashboard's client-side slug check but the backend 400s it).
    code, dup = req(f"{DASH}/api/channels", {"name": "feed", "body": "nope"},
                    opener=_dash_opener.open)
    check("dashboard surfaces backend rejection verbatim",
          code == 400 and "reserved" in str(dup), str(dup))

    # organizer broadcast still works alongside channels
    code, bc = req(f"{DASH}/api/messages", {"body": "broadcast check", "refs": [], "broadcast": True},
                   opener=_dash_opener.open)
    check("organizer broadcast unaffected", code == 200 and bc.get("broadcast") is True, str(bc)[:200])

    # ── watch/long-poll dashboard proxies (WATCH_DESIGN.md §10) ──
    code, watching = req(f"{DASH}/api/watching")
    check("GET /api/watching proxies presence + longpoll stats",
          code == 200 and {"max_wait_s", "fresh_s", "watching", "longpoll"} <= set(watching),
          str(watching)[:200])

    # THE key invariant (§10.2): the proxy forces wait=0 no matter what the
    # caller asks for, so a parked browser connection can never eat one of the
    # backend's bounded waiter slots. A regression that re-forwards `wait`
    # would pass the whole rest of the suite silently — only this timing
    # assertion pins it.
    t0 = time.time()
    code, um = req(f"{DASH}/api/updates?as=byte-bandit&wait=30")
    dt = time.time() - t0
    check("GET /api/updates strips wait= (returns fast, no watch block)",
          code == 200 and dt < EARLY_S and not um.get("watch"),
          f"elapsed={dt:.2f}s {str(um)[:160]}")

    # Subscribe via the dashboard proxy (invalidates the caller's own
    # notify-levels cache entry, app.py) on a channel the logged-in human is
    # already a member of (creator of context-strategies above), then confirm
    # the level shows up without waiting out the cache TTL.
    code, sub = req(f"{DASH}/api/channels/context-strategies/subscribe", {"notify": "all"},
                    opener=_dash_opener.open)
    check("dashboard subscribe proxy sets notify: all",
          code == 200 and sub.get("notify") == "all", str(sub))
    code, levels = req(f"{DASH}/api/notify-levels", opener=_dash_opener.open)
    check("notify-levels proxy reflects the level immediately (cache invalidated)",
          code == 200 and levels.get("levels", {}).get("context-strategies") == "all",
          str(levels))

    code, page = req(f"{DASH}/")
    check("SPA serves", code == 200 and "channelChips" in page)


def watch_flows() -> None:
    """WATCH_DESIGN.md §11 — the long-poll surface end-to-end.

    Runs last: it posts extra board/channel traffic and flips a notification
    level, so keeping it after the channels flows leaves their counts alone.
    """
    print("backend — watch (long-poll) flows")
    me, them = "byte-bandit", "delta-coder"
    src = {"source": f"hf://buckets/{ORG}/{SLUG}-{me}/subscribe.md"}
    ch = "watch-lab"

    # The waiter registry's own observability — and the canary for the whole
    # section: every wait= route resolves get_notifier, so a 500 here means
    # nothing below can pass.
    code, health = req(f"{API}/v1/healthz")
    lp = health.get("longpoll") if isinstance(health, dict) else None
    check("healthz exposes the longpoll waiter registry counters",
          code == 200 and isinstance(lp, dict)
          and {"waiters", "owners", "parks", "wakes", "evictions", "degradations"} <= set(lp),
          f"{code} {health}")

    code, doc = req(f"{API}/v1")
    paths = {e["path"] for e in doc.get("endpoints", [])}
    check("discovery lists the watch endpoints + wait= polling note",
          {"/v1/updates", "/v1/watch.sh"} <= paths
          and "wait=55" in doc["conventions"]["polling"], str(sorted(paths))[:120])

    code, created = req(f"{API}/v1/channels", {
        "name": ch, "agent_id": "human-tester",
        "body": "Long-poll lab: parked polls, notify levels, and what wakes a watcher.",
    }, bearer="any-token")
    check("watch-lab channel created (organizer)", code == 201 and created.get("created") is True,
          str(created))

    # ── §4.1: wait=0 changes nothing; wait+before is a bug worth naming ──
    code, plain = req(f"{API}/v1/updates?as={me}&expand=true&limit=5")
    code0, zero = req(f"{API}/v1/updates?as={me}&expand=true&limit=5&wait=0")
    check("wait=0 leaves the same parsed response (no watch block)",
          code == 200 and code0 == 200 and zero == plain and zero.get("watch") is None
          and {"count", "matched", "items", "next", "cursor"} <= set(zero), str(zero)[:200])

    anchor = plain["items"][0]["filename"] if plain["items"] else "20260101-000000-000_x.md"
    code, e1 = req(f"{API}/v1/updates?as={me}&wait=5&before={anchor}")
    code2, e2 = req(f"{API}/v1/inbox/{me}?wait=5&before={anchor}")
    check("wait + before rejected on updates and inbox (400 INVALID_QUERY)",
          code == 400 and e1["error"]["code"] == "INVALID_QUERY"
          and code2 == 400 and e2["error"]["code"] == "INVALID_QUERY", f"{code}/{code2} {e1}")

    # ── §4.2: a parked poll returns the moment a mention lands ──
    cursor = plain.get("cursor")
    park = Park(stream_url("updates", me, after=cursor, order="asc", expand="true",
                           limit=10, wait=10))
    park.settle()
    code, msg = req(f"{API}/v1/messages", {"agent_id": them, "body": f"@{me} parked-poll delivery"})
    items, watch, status = park.finish()
    check("parked wait=10 updates poll wakes early on a mention (item, reasons, cursor)",
          code == 201 and park.elapsed < EARLY_S and status == "delivered"
          and [i["filename"] for i in items] == [msg["filename"]]
          and items[0].get("reasons") == ["mention"]
          and park.doc.get("cursor") == msg["filename"],
          f"elapsed={park.elapsed:.2f}s watch={watch} items={[i['filename'] for i in items]}")
    cursor = park.doc.get("cursor") or cursor

    # ── §4.3: notification levels decide what wakes you ──
    code, sub = req(f"{API}/v1/channels/{ch}/subscribe", src)
    check("subscribe defaults to the quiet level (notify: mentions)",
          code == 200 and sub.get("notify") == "mentions" and sub.get("changed") is True, str(sub))

    park = Park(stream_url("updates", me, after=cursor, order="asc", expand="true",
                           limit=10, wait=4))
    park.settle()
    code, quiet = req(f"{API}/v1/messages", {
        "agent_id": them, "channel": ch, "body": "plain channel post, nobody mentioned"})
    items, watch, status = park.finish()
    check("mentions-level channel: a plain post does NOT resolve a parked poll (times out empty)",
          code == 201 and items == [] and status == "timeout" and park.elapsed >= 3.5,
          f"elapsed={park.elapsed:.2f}s watch={watch} items={items}")

    park = Park(stream_url("updates", me, after=cursor, order="asc", expand="true",
                           limit=10, wait=10))
    park.settle()
    code, msg = req(f"{API}/v1/messages", {
        "agent_id": them, "channel": ch, "body": f"@{me} mention inside a quiet channel"})
    items, watch, status = park.finish()
    check("mentions-level channel: an @mention in it DOES resolve it (via the inbox side)",
          code == 201 and park.elapsed < EARLY_S and status == "delivered"
          and [i["filename"] for i in items] == [msg["filename"]]
          and items[0].get("reasons") == ["mention"],
          f"elapsed={park.elapsed:.2f}s watch={watch} items={items}")
    cursor = park.doc.get("cursor") or cursor

    code, sub = req(f"{API}/v1/channels/{ch}/subscribe", {**src, "notify": "all"})
    check("re-subscribing with notify: all is the level change (changed: true)",
          code == 200 and sub.get("notify") == "all" and sub.get("changed") is True, str(sub))

    park = Park(stream_url("updates", me, after=cursor, order="asc", expand="true",
                           limit=10, wait=10))
    park.settle()
    code, msg = req(f"{API}/v1/messages", {
        "agent_id": them, "channel": ch, "body": "plain post, now at notify: all"})
    items, watch, status = park.finish()
    check("notify: all merges plain channel traffic into /v1/updates (reasons: channel:<name>)",
          code == 201 and park.elapsed < EARLY_S and status == "delivered"
          and [i["filename"] for i in items] == [msg["filename"]]
          and items[0].get("reasons") == [f"channel:{ch}"],
          f"elapsed={park.elapsed:.2f}s watch={watch} items={items}")
    cursor = park.doc.get("cursor") or cursor

    # ── §4.4: a timeout is a 200 that says so ──
    t0 = time.time()
    code, doc = get(stream_url("updates", me, after=cursor, order="asc", expand="true", wait=2))
    dt = time.time() - t0
    check("short wait, nothing posted: empty page + watch.status=timeout",
          code == 200 and (doc.get("items") or []) == [] and doc.get("cursor") is None
          and (doc.get("watch") or {}).get("status") == "timeout" and 1.5 <= dt < 6,
          f"elapsed={dt:.2f}s {str(doc)[:200]}")

    # ── §3.2.2: nothing to park on is answered at once, not after 55s ──
    t0 = time.time()
    code, doc = get(stream_url("feed", "human-nobody", wait=5, expand="true"))
    dt = time.time() - t0
    check("feed with an empty key set returns immediately (watch.status=no_streams)",
          code == 200 and (doc.get("items") or []) == [] and dt < 2
          and (doc.get("watch") or {}).get("status") == "no_streams",
          f"elapsed={dt:.2f}s {str(doc)[:200]}")

    # ── §5.5: the server half of cursor integrity ──
    scratch = BUCKETS / ORG / f"{SLUG}-{me}" / "drafts"
    scratch.mkdir(parents=True, exist_ok=True)
    (scratch / "cursor-pin.md").write_text(
        "---\ntype: note\nfilename: 99999999-235959-999_zzz.md\n---\n"
        "a frontmatter key that would pin every watcher's cursor past all future mail\n")
    code, rej = req(f"{API}/v1/messages",
                    {"source": f"hf://buckets/{ORG}/{SLUG}-{me}/drafts/cursor-pin.md"})
    check("disallowed frontmatter key rejected (400 INVALID_FRONTMATTER, names the key)",
          code == 400 and rej["error"]["code"] == "INVALID_FRONTMATTER"
          and "'filename'" in rej["error"]["message"], f"{code} {rej}")

    # ── §4.5: the digest answers "am I behind?" and "is anyone watching?" ──
    code, newest = req(f"{API}/v1/updates?as={me}&limit=1&order=desc")
    code, dg = req(f"{API}/v1/digest?as={me}&after=")  # empty: count the whole stream
    up = dg.get("updates") or {}
    code, dg_caught = req(f"{API}/v1/digest?as={me}&after={up.get('newest')}")
    check("digest updates block: unread is cursor-aware, newest matches the stream",
          up.get("unread", 0) > 0 and up.get("newest") == newest.get("cursor")
          and (dg_caught.get("updates") or {}).get("unread") == 0,
          f"{up} vs stream cursor {newest.get('cursor')} / after= {dg_caught.get('updates')}")
    watching = dg.get("watching") or {}
    check("digest watching block is live right after an updates read",
          watching.get("stream") == "updates" and watching.get("mode") == "poll"
          and 0 <= watching.get("last_poll_age_s", -1) < 120,
          str(dg.get("watching")))
    levels = {c["name"]: c.get("notify") for c in (dg["channels"].get("subscribed") or [])}
    check("digest reports each membership's notify level",
          levels.get(ch) == "all" and levels.get("eval-harness") == "mentions", str(levels))
    # A handle no wait>0 poll has ever named (human-nobody above parked a feed
    # poll, so it is legitimately "watched" and would not prove anything).
    code, nobody = req(f"{API}/v1/digest?as=human-unwatched")
    check("digest watching is null for a handle nobody has ever watched",
          code == 200 and nobody.get("watching") is None, str(nobody.get("watching")))

    # ── §4.6: the client is served by the server it talks to ──
    code, body, ctype = raw_get(f"{API}/v1/watch.sh")
    check("GET /v1/watch.sh serves the client script (200, shell-shaped, matches disk)",
          code == 200 and body.startswith("#!/bin/sh") and len(body) > 4000
          and "x-shellscript" in ctype and body == WATCH_SH.read_text(),
          f"{code} {ctype} {len(body)}B")

    watch_client_flows(me, them)


def watch_client_flows(me: str, them: str) -> None:
    """collab_watch.sh (§5) driven exactly as an agent's harness would, against
    the live stack. Every run gets a temp COLLAB_WATCH_DIR, so nothing touches
    $HOME and the runs leave no state behind."""
    print("client — collab_watch.sh end-to-end")
    tmp = Path(tempfile.mkdtemp(prefix="collab-watch-smoke-"))
    try:
        st = tmp / "state"
        cursor_file = st / "cursor.updates"

        # Cold start: baseline the newest existing filename WITHOUT printing it,
        # then time out cleanly — a fresh watcher must never dump history.
        r = watch_run(st, me, "--max-wait", "2", wait="1", timeout=30)
        base = cursor_file.read_text().strip() if cursor_file.exists() else ""
        check("cold start baselines the cursor, prints nothing, exits 3 (clean no-mail)",
              r.returncode == 3 and r.stdout == "" and base
              and "cold start" in r.stderr, f"rc={r.returncode} out={r.stdout[:80]!r} cursor={base!r}")

        # A real delivery: journal, print, advance.
        p = watch_bg(st, me, "--max-wait", "12", wait="6")
        time.sleep(1.0)
        code, msg = req(f"{API}/v1/messages", {"agent_id": them, "body": f"@{me} client delivery"})
        out, err = p.communicate(timeout=40)
        journal = st / "delivered.jsonl"
        check("a delivery prints the page, journals it, advances the cursor, exits 0",
              p.returncode == 0 and msg["filename"] in out
              and cursor_file.read_text().strip() == msg["filename"]
              and journal.exists() and len(journal.read_text().strip().splitlines()) == 1,
              f"rc={p.returncode} cursor={cursor_file.read_text().strip()} err={err[-160:]!r}")

        # --peek reports what is pending and leaves the cursor exactly where it was.
        code, pending = req(f"{API}/v1/messages", {"agent_id": them, "body": f"@{me} client peek probe"})
        r = watch_run(st, me, "--peek", wait="1", timeout=30)
        check("--peek reports pending mail (exit 10) without advancing the cursor",
              r.returncode == 10 and pending["filename"] in r.stdout
              and cursor_file.read_text().strip() == msg["filename"],
              f"rc={r.returncode} cursor={cursor_file.read_text().strip()}")

        r = watch_run(st, me, "--status", wait="1", timeout=30)
        check("--status reports BEHIND with the unread count (exit 10)",
              r.returncode == 10 and "STATUS=BEHIND" in r.stdout and "UNREAD=1" in r.stdout,
              f"rc={r.returncode} {r.stdout.strip()!r}")

        r = watch_run(st, me, "--max-wait", "5", wait="2", timeout=40)
        check("the next run drains the pending page and moves the cursor to it",
              r.returncode == 0 and cursor_file.read_text().strip() == pending["filename"],
              f"rc={r.returncode} cursor={cursor_file.read_text().strip()}")

        # A live watcher: --status can see it, and a second one must not share
        # its cursor file (the eq2 double-delivery failure).
        p = watch_bg(st, me, "--max-wait", "6", wait="6")
        time.sleep(1.5)
        r = watch_run(st, me, "--status", wait="6", timeout=30)
        check("--status reports OK (exit 0) while a watcher holds the lock and is caught up",
              r.returncode == 0 and "STATUS=OK" in r.stdout and f"PID={p.pid}" in r.stdout,
              f"rc={r.returncode} {r.stdout.strip()!r}")
        r = watch_run(st, me, "--max-wait", "2", wait="6", timeout=30)
        check("a second watcher on the same state dir exits 5 naming the holder's pid",
              r.returncode == 5 and "another watcher" in r.stderr and str(p.pid) in r.stderr,
              f"rc={r.returncode} {r.stderr.strip()[-160:]!r}")
        out, err = p.communicate(timeout=40)
        check("the bounded watcher exits 3 on its own and releases the lock",
              p.returncode == 3 and not (st / "lock").exists(), f"rc={p.returncode}")
        r = watch_run(st, me, "--status", wait="1", timeout=30)
        check("--status reports NO_WATCHER (exit 11) once the watcher is gone",
              r.returncode == 11 and "STATUS=NO_WATCHER" in r.stdout,
              f"rc={r.returncode} {r.stdout.strip()!r}")

        # A typo'd handle is not a transient condition: fail fast, print the body.
        r = watch_run(tmp / "bogus", "no-such-agent", "--max-wait", "3", wait="1", timeout=30)
        check("a bogus handle fails fast (exit 1) printing the server's error body",
              r.returncode == 1 and "NOT_REGISTERED" in r.stderr and r.stdout == "",
              f"rc={r.returncode} {r.stderr.strip()[-160:]!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    t0 = time.time()
    backend_agent_flows()
    dashboard_flows()
    watch_flows()
    dt = time.time() - t0
    print(f"\n{_n - len(failures)}/{_n} checks passed in {dt:.1f}s")
    if failures:
        print("failures:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
