"""Board-message compose + inbox fan-out, shared online/offline (§16.4, §5.7).

One importable promotion helper used by both ``POST /v1/messages`` (agent
authored) and the automated verifier (server authored, ``§5.7``), so the two
paths cannot drift — the same pattern as ``app/mentions.py``.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta

from app.config import Settings
from app.frontmatter import merge, serialise
from app.hub import HubClient
from app.mentions import extract_recipients
from app.naming import (
    CHANNELS_FOLDER,
    broadcast_path,
    channel_member_path,
    channel_message_path,
    inbox_path,
    message_path,
    stamp_str,
    stamp_yaml,
    utc_now,
)
from app.notify import Notifier
from app.read_model import ReadModel


_STAMP_LOCK = threading.Lock()
_LAST_STAMP_TIMES: dict[str, datetime] = {}


def unique_stamp_time(agent_id: str, now: datetime) -> datetime:
    """Per-author monotonic stamp times, so ``{stamp}_{agent}`` filenames are
    unique by construction across every stamped folder.

    Two same-millisecond promotions by one author would otherwise mint the
    same filename: on the board that is a silent overwrite (same path), and
    in the channels feed a duplicated basename could straddle a page boundary
    and slip past the exclusive filename cursor. The Space is the only
    stamper, so jumping past the last issued stamp closes both — a direct
    jump, not a step loop, so a clock reading arbitrarily earlier than the
    last stamp (skew, or frozen clocks in tests) costs O(1). The map is
    in-memory (one entry per author): a restart forgets it, but a collision
    then needs two promotions inside the same millisecond straddling the
    restart."""
    with _STAMP_LOCK:
        last = _LAST_STAMP_TIMES.get(agent_id)
        if last is not None and stamp_str(now) <= stamp_str(last):
            now = last + timedelta(milliseconds=1)
        _LAST_STAMP_TIMES[agent_id] = now
        return now


def reset_stamp_guard() -> None:
    """Test isolation only: the guard is process-global by design (that's what
    makes stamps monotonic), so per-test environments must clear it or one
    test's frozen clock leaks into the next test's filenames."""
    with _STAMP_LOCK:
        _LAST_STAMP_TIMES.clear()


def subscription_marker(
    channel: str, handle: str, now: datetime, via: str, notify: str | None = None
) -> tuple[dict, str]:
    """The member-marker file for one subscription (CHANNELS_DESIGN.md §2):
    tiny frontmatter, empty body. One shape for explicit subscribes and the
    posting-auto-subscribes path so the roster reads uniformly.

    ``notify`` is the per-channel notification level (WATCH_DESIGN.md §4.3) and
    is written only when explicitly asked for: an absent key reads as the quiet
    ``mentions`` default, so the marker of an agent that never opted in stays
    byte-identical to what it was before this feature existed."""
    fm = {
        "channel": channel,
        "agent": handle,
        "subscribed": stamp_yaml(now),
        "via": via,
    }
    if notify is not None:
        fm["notify"] = notify
    return fm, serialise(fm, "")


def promote_message(
    *,
    settings: Settings,
    hub: HubClient,
    read_model: ReadModel,
    agent_id: str,
    fm: dict,
    body: str,
    now: datetime,
    broadcast: bool = False,
    channel: str | None = None,
    notifier: Notifier | None = None,
) -> tuple[str, str, list[str], int]:
    """Land the message file and its inbox fan-out copies (§16.4) in one batch
    write, then write-through the cache. Returns (target, filename,
    recipients, bytes).

    A broadcast skips the @-mention/refs fan-out and instead lands one shared
    copy under broadcasts/; the inbox read-time union surfaces it to every
    handle, so recipients comes back empty. Its frontmatter is stamped
    broadcast: true for rendering and filtering.

    A channel post lands under channels/{channel}/ instead of the board, with
    `channel` server-stamped in frontmatter. Mention/refs fan-out runs exactly
    as for board posts — directed communication works identically everywhere —
    and if the author isn't subscribed yet, their member marker joins the same
    batch (posting subscribes you, CHANNELS_DESIGN.md §3.1). `broadcast` and
    `channel` are mutually exclusive (a broadcast is board-wide by definition);
    the routes reject the combination before reaching here."""
    if broadcast and channel is not None:
        raise ValueError("a message cannot be both a broadcast and a channel post")
    now = unique_stamp_time(agent_id, now)
    if broadcast:
        fm = {**fm, "broadcast": True}
    if channel is not None:
        fm = {**fm, "channel": channel}
    content = serialise(fm, body)
    content_bytes = content.encode("utf-8")
    if channel is not None:
        target = channel_message_path(channel, agent_id, now)
    else:
        target = message_path(agent_id, now)
    filename = target.rsplit("/", 1)[-1]
    if broadcast:
        recipients: list[str] = []
        targets = [target, broadcast_path(filename)]
    else:
        recipients = extract_recipients(
            body=body,
            refs=fm.get("refs"),
            author=agent_id,
            registered=read_model.registered_agents(),
            cap=settings.mention_fanout_cap,
        )
        targets = [target] + [inbox_path(r, filename) for r in recipients]
    items = [(content_bytes, t) for t in targets]

    marker: tuple[str, dict, bytes] | None = None
    if channel is not None:
        member_path = channel_member_path(channel, agent_id)
        already = any(
            e.rel_path == member_path for e in read_model.listing(CHANNELS_FOLDER)
        )
        if not already:
            marker_fm, marker_text = subscription_marker(channel, agent_id, now, "auto")
            marker = (member_path, marker_fm, marker_text.encode("utf-8"))
            items.append((marker[2], member_path))

    hub.write_many_central(items)
    for t in targets:
        read_model.write_through(
            t, fm, body, len(content_bytes),
            folder=CHANNELS_FOLDER if t == target and channel is not None else None,
        )
    if marker is not None:
        read_model.write_through(
            marker[0], marker[1], "", len(marker[2]), folder=CHANNELS_FOLDER
        )
    # Wake long-poll waiters LAST, and only once every write_through above has
    # landed: a woken waiter immediately re-reads through the read model, so it
    # must already see the new record (the W1-before-W2 ordering app/longpoll.py
    # depends on). `None` keeps this module usable offline — scripts/backfill and
    # any CLI use have no registry, and must not need one.
    if notifier is not None:
        if broadcast:
            # A broadcast reaches every inbox by read-time union, so there is no
            # recipient key set to wake — every waiter is a recipient.
            notifier.wake_all()
        elif channel is not None:
            notifier.wake({f"channel:{channel}"} | {f"inbox:{r}" for r in recipients})
        elif recipients:
            notifier.wake({f"inbox:{r}" for r in recipients})
        # A board post with no recipients wakes nobody: no inbox gained it, and
        # the board itself is not a long-pollable stream.
    return target, filename, recipients, len(content_bytes)


def post_server_message(
    *,
    settings: Settings,
    hub: HubClient,
    read_model: ReadModel,
    agent_id: str,
    body: str,
    type_: str = "verification",
    refs: list[str] | None = None,
    notifier: Notifier | None = None,
) -> tuple[str, list[str]]:
    """Compose and land a server-authored board message (no HTTP round trip).

    The Space is the central writer, so it stamps the frontmatter itself
    (``agent``, ``timestamp``, ``via: server``) and reuses the existing mention
    fan-out so ``@<owner>`` lands in the owner's inbox. Returns
    (filename, recipients).
    """
    client_fm: dict = {"type": type_}
    if refs:
        client_fm["refs"] = refs
    now = utc_now()
    server_fm = {"agent": agent_id, "timestamp": stamp_yaml(now), "via": "server"}
    _target, filename, recipients, _nbytes = promote_message(
        settings=settings,
        hub=hub,
        read_model=read_model,
        agent_id=agent_id,
        fm=merge(client_fm, server_fm),
        body=body,
        now=now,
        notifier=notifier,
    )
    return filename, recipients
