#!/usr/bin/env python3
"""Offline, idempotent inbox backfill / reconcile (§16.4).

Run by an admin whose HF token can write the central bucket (contributors
cannot — their writes are mediated by the Space):

    HF_TOKEN=hf_... python scripts/backfill_inbox.py [--dry-run] [--chunk 500]

Lists message_board/, agents/ and inbox/, downloads every message, runs the
SAME recipient extraction as the live fan-out (app.mentions — one shared
function, so online and offline behavior cannot drift), and writes the
missing byte-identical copies to inbox/{recipient}/{filename}. Copies that
already exist are skipped, so re-running is always safe — after a regex
change, a failed fan-out write, or a late registration that should pick up
old mentions.

Deploy order at feature launch: ship the live fan-out first, then run this —
the overlap window is then covered with zero coordination.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.frontmatter import parse  # noqa: E402
from app.hub import HubClient  # noqa: E402
from app.mentions import extract_recipients  # noqa: E402
from app.naming import agent_from_filename, inbox_path  # noqa: E402


_README_RE = re.compile(r"(?:^|/)README\.md$", re.IGNORECASE)


def _md_basenames(hub: HubClient, folder: str) -> list:
    return [
        e
        for e in hub.list_central_dir(folder)
        if e.rel_path.endswith(".md") and not _README_RE.search(e.rel_path)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Backfill / reconcile inbox fan-out copies from message_board/."
    )
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--chunk", type=int, default=500, help="files per batch write")
    args = ap.parse_args()

    settings = get_settings()
    hub = HubClient(settings)

    messages = _md_basenames(hub, "message_board")
    registered = {
        e.rel_path.rsplit("/", 1)[-1].removesuffix(".md")
        for e in _md_basenames(hub, "agents")
    }
    existing = {e.rel_path for e in hub.list_central_dir("inbox")}
    print(
        f"messages={len(messages)} registered_agents={len(registered)} "
        f"existing_inbox_copies={len(existing)}"
    )

    raw_by_path = hub.download_many(
        settings.central_bucket, [e.rel_path for e in messages]
    )
    failed_downloads = len(messages) - len(raw_by_path)
    if failed_downloads:
        print(f"WARNING: {failed_downloads} message(s) failed to download; re-run to cover them")

    planned: list[tuple[bytes, str]] = []
    unparseable = 0
    deliveries = 0
    for e in messages:
        raw = raw_by_path.get(e.rel_path)
        if raw is None:
            continue
        filename = e.rel_path.rsplit("/", 1)[-1]
        author = agent_from_filename(filename) or ""
        try:
            fm, body = parse(raw.decode("utf-8"))
        except Exception:
            unparseable += 1
            continue
        recipients = extract_recipients(
            body=body,
            refs=fm.get("refs"),
            author=author,
            registered=registered,
            cap=settings.mention_fanout_cap,
        )
        deliveries += len(recipients)
        for r in recipients:
            dest = inbox_path(r, filename)
            if dest not in existing:
                planned.append((raw, dest))

    print(
        f"deliveries_expected={deliveries} already_present={deliveries - len(planned)} "
        f"to_write={len(planned)} unparseable_skipped={unparseable}"
    )
    if args.dry_run:
        for _, dest in planned[:50]:
            print(f"  would write {dest}")
        if len(planned) > 50:
            print(f"  … and {len(planned) - 50} more")
        return 0

    for start in range(0, len(planned), args.chunk):
        chunk = planned[start : start + args.chunk]
        hub.write_many_central(chunk)
        print(f"wrote {start + len(chunk)}/{len(planned)}")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
