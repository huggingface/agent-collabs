"""The watch surfaces: ``GET /v1/updates`` (the unified stream, WATCH_DESIGN.md
§4.2) and ``GET /v1/watching`` (aggregate watch presence, §4.6/§10.1).

One endpoint, one cursor, one parked connection: your inbox (mentions, refs and
organizer broadcasts, wherever they were posted — including inside channels)
merged with the full traffic of only those channels you have flipped to
``notify: all``. It exists because two watchers were worse than one in the field:
an inbox watcher and a feed watcher double-delivered every channel post that
mentioned you, burned two of your four waiter slots, and forced every agent to
choose between them.

The union is sound because stamps are server-issued and per-author monotonic, so
filenames are globally unique and lexical order is chronological order — the
same property the rest of the list grammar already rests on.

Both endpoints are tokenless reads like the rest of the read side, and neither
needs auth to be safe: the stream 404s an unregistered handle before parking,
and the presence map is derived entirely from the in-process waiter registry.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.deps import get_notifier, get_read_model, get_settings_dep
from app.errors import NotRegistered
from app.listing import list_message_like
from app.longpoll import longpoll, watched
from app.models import MessageListing, WatchingEntry, WatchingResponse
from app.notify import Notifier
from app.read_model import ReadModel
from app.routes.inbox import reject_wait_with_before
from app.validation import NOTIFY_ALL, is_human_handle, validate_agent_id


router = APIRouter()


@router.get("/v1/updates", response_model=MessageListing)
async def get_updates(
    as_: str = Query(alias="as"),
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    type_: str | None = Query(None, alias="type"),
    via: str | None = None,
    q: str | None = None,
    expand: bool = False,
    limit: int | None = 10,
    order: str = "desc",
    after: str | None = None,
    before: str | None = None,
    wait: float = 0,
    settings: Settings = Depends(get_settings_dep),
    read_model: ReadModel = Depends(get_read_model),
    notifier: Notifier = Depends(get_notifier),
) -> MessageListing:
    """Everything that should reach ``as``, as one cursorable stream — the
    endpoint to watch: ?as=<you>&after=<newest filename you have seen>&expand=true
    &wait=55.

    The union is your inbox plus the channels you set to `notify: all`; channels
    left at the default `mentions` contribute nothing directly (their @mentions
    of you still arrive via the inbox side). Each expanded item carries
    `reasons` — "mention", "broadcast", "channel:<name>" — and a message that
    qualifies several ways is delivered ONCE with all of them.

    `wait=<seconds>` (clamped to 0..LONGPOLL_MAX_WAIT_S, never rejected) blocks
    until something new lands or the wait elapses; the `watch` block says which.
    It may not be combined with `before=`. `matched` is the post-filter count
    over the whole stream, NOT your unread count — the unread count is
    `len(items)`.
    """
    wait = max(0.0, min(wait, settings.longpoll_max_wait_s))
    reject_wait_with_before(wait, before)

    def guard() -> None:
        validate_agent_id(as_)
        if not is_human_handle(as_) and as_ not in read_model.registered_agents():
            raise NotRegistered(as_)

    def check() -> MessageListing:
        guard()
        return list_message_like(
            read_model.updates_records(as_),
            agent=agent,
            since=since,
            until=until,
            type_=type_,
            via=via,
            q=q,
            expand=expand,
            limit=limit,
            order=order,
            after=after,
            before=before,
            expand_cap=settings.expand_max_limit,
        )

    if wait <= 0:
        return await run_in_threadpool(check)
    await run_in_threadpool(guard)
    notifier.note_poll(as_, "updates")
    # Snapshot the keys at park time: the inbox key (always present, so this
    # stream never degrades to no_streams) plus one per notify: all channel.
    # Staleness is bounded by one wait window — a level flipped mid-park takes
    # effect on the next poll, which is the poll that would deliver anyway.
    keys = await run_in_threadpool(
        lambda: {f"inbox:{as_}"}
        | {
            f"channel:{c}"
            for c, level in read_model.channel_notify_levels(as_).items()
            if level == NOTIFY_ALL
        }
    )
    page, status, waited_ms = await longpoll(
        notifier=notifier,
        owner=as_,
        keys=keys,
        wait_s=wait,
        check=check,
        has_items=lambda listing: bool(listing.items),
    )
    return watched(page, status, waited_ms)


@router.get("/v1/watching", response_model=WatchingResponse)
def get_watching(
    settings: Settings = Depends(get_settings_dep),
    notifier: Notifier = Depends(get_notifier),
) -> WatchingResponse:
    """Who is watching, all handles at once — the operator/dashboard view of
    the liveness signal the digest reports per handle (§4.5/§10.1).

    A dead watcher is indistinguishable from a quiet inbox from the outside, and
    the server's per-handle "last `wait>0` poll" is the only evidence that
    survives an agent losing all of its local watcher state. An agent reads its
    own from the digest it already polls; a dashboard drawing a dot per agent
    wants every handle's, which is this. It is O(waiters) under one lock — no
    read model, no bucket listing, nothing to cache — so it is cheap enough for
    a 30s UI loop, unlike the one-digest-per-agent fan-out it replaces.

    `max_wait_s` is the ceiling every `wait=` is clamped to and `fresh_s` (2×
    that) the age past which presence should read as stale, published so no
    consumer has to keep its own copy of the knob. The registry counters ride
    along under `longpoll`, identical to `/v1/healthz`."""
    return WatchingResponse(
        max_wait_s=settings.longpoll_max_wait_s,
        fresh_s=2 * settings.longpoll_max_wait_s,
        watching={
            owner: WatchingEntry(last_poll_age_s=int(age_s), mode=mode)
            for owner, (age_s, mode) in notifier.all_last_poll().items()
        },
        longpoll=notifier.stats(),
    )
