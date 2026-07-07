#!/usr/bin/env python3
"""End-to-end smoke of the channels feature against a running testenv
(./testenv/up.sh). Stdlib only — no deps, no venv:

    python3 testenv/smoke.py

Exercises the backend as agents (raw + bucket-source + subscribe proofs) and
the dashboard as a fake-logged-in human (proxies, composer channel post,
creation endpoint). Prints one line per check; exits non-zero on any failure.
"""
from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "http://127.0.0.1:8100"
DASH = "http://127.0.0.1:7861"
ROOT = Path(__file__).resolve().parent.parent
BUCKETS = ROOT / ".testenv" / "buckets"
ORG, SLUG = "local-org", "collab"

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


def backend_agent_flows() -> None:
    print("backend — agent flows")

    code, doc = req(f"{API}/v1")
    paths = {e["path"] for e in doc.get("endpoints", [])}
    check("discovery lists channel endpoints",
          {"/v1/channels", "/v1/channels/feed"} <= paths and "channels" in doc.get("conventions", {}))

    # create (agent, raw) → auto-announce on the board
    code, ch = req(f"{API}/v1/channels", {
        "name": "eval-harness", "agent_id": "byte-bandit",
        "body": "Scoring, verification, and how to not fool ourselves. Bring measurements.",
    })
    check("create channel as agent (201 + announcement)",
          code == 201 and ch.get("created") is True and ch.get("announcement"), str(ch))
    ann = ch.get("announcement")

    code, board = req(f"{API}/v1/messages?expand=true&limit=50")
    files = {m["filename"] for m in board["items"]}
    check("announcement is a board message", ann in files)

    code, dup = req(f"{API}/v1/channels", {"name": "eval-harness", "agent_id": "delta-coder", "body": "mine"})
    check("duplicate create by another agent → 409 CHANNEL_EXISTS",
          code == 409 and dup["error"]["code"] == "CHANNEL_EXISTS", str(dup))
    code, _ = req(f"{API}/v1/channels", {"name": "feed", "agent_id": "byte-bandit", "body": "x"})
    check("reserved name 'feed' → 400", code == 400)
    code, _ = req(f"{API}/v1/channels", {"name": "empty", "agent_id": "byte-bandit", "body": "  "})
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
    check("human creates channel with Bearer (via dashboard path)",
          code == 201 and human.get("via") == "dashboard", str(human))


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
    # …while claiming an agent-created channel surfaces the backend 409 verbatim.
    code, dup = req(f"{DASH}/api/channels", {"name": "eval-harness", "body": "mine now"},
                    opener=_dash_opener.open)
    check("dashboard surfaces backend 409 verbatim", code == 409 and "already exists" in str(dup), str(dup))

    # organizer broadcast still works alongside channels
    code, bc = req(f"{DASH}/api/messages", {"body": "broadcast check", "refs": [], "broadcast": True},
                   opener=_dash_opener.open)
    check("organizer broadcast unaffected", code == 200 and bc.get("broadcast") is True, str(bc)[:200])

    code, page = req(f"{DASH}/")
    check("SPA serves", code == 200 and "channelChips" in page)


def main() -> None:
    t0 = time.time()
    backend_agent_flows()
    dashboard_flows()
    dt = time.time() - t0
    print(f"\n{_n - len(failures)}/{_n} checks passed in {dt:.1f}s")
    if failures:
        print("failures:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
