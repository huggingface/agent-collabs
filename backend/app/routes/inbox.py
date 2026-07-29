from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.deps import get_notifier, get_read_model, get_settings_dep
from app.errors import InvalidQuery, NotRegistered
from app.listing import list_message_like
from app.longpoll import longpoll, watched
from app.models import MessageListing
from app.notify import Notifier
from app.read_model import ReadModel
from app.validation import is_human_handle, validate_agent_id


router = APIRouter()


def reject_wait_with_before(wait: float, before: str | None) -> None:
    """The one guard `wait=` adds to the list grammar, shared by every
    long-pollable stream: a before-cursor page looks BACKWARD, so it can never
    gain items and the wait could never resolve early — a caller passing both
    has a bug we should name rather than a 55s stall we should serve."""
    if wait > 0 and before is not None:
        raise InvalidQuery(
            "`wait` cannot be combined with `before`",
            "a before-cursor page can never gain new items, so the wait could "
            "never resolve early; drop one of them",
        )


@router.get("/v1/inbox/{handle}", response_model=MessageListing)
async def get_inbox(
    handle: str,
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
    """Messages that mention or `refs` the handle (§16.4) — fan-out copies
    under inbox/{handle}/, same grammar as /v1/messages. The canonical polling
    loop is one call: ?after=<newest filename you have seen>&expand=true
    (exclusive cursor, so the boundary message is never re-delivered).

    `handle` is an agent_id or a human-<name> handle; humans never register,
    so only agent handles get the registration check. `agent=` filters by the
    *author* of the copied message.

    `wait=<seconds>` (clamped to 0..LONGPOLL_MAX_WAIT_S, never rejected) blocks
    until a new message lands for the handle or the wait elapses, returning the
    same listing shape either way plus a `watch` block saying which happened; it
    may not be combined with `before=`. `matched` is the post-filter count over
    the whole inbox, NOT your unread count — the unread count is `len(items)`.
    """
    wait = max(0.0, min(wait, settings.longpoll_max_wait_s))
    reject_wait_with_before(wait, before)

    def guard() -> None:
        validate_agent_id(handle)
        if not is_human_handle(handle) and handle not in read_model.registered_agents():
            raise NotRegistered(handle)

    # The exact production query — validation, registration check, and the
    # listing — as one blocking closure so every read-model touch runs off the
    # event loop (a cold miss can hit the network).
    def check() -> MessageListing:
        guard()
        return list_message_like(
            read_model.inbox_records(handle),
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
    # Registration is checked BEFORE anything is registered or recorded, so a
    # fabricated handle 404s without taking a waiter slot or stamping a watch
    # presence for a name that does not exist (§2, §7).
    await run_in_threadpool(guard)
    notifier.note_poll(handle, "inbox")
    # Broadcasts arrive via wake_all, so the single inbox key suffices.
    page, status, waited_ms = await longpoll(
        notifier=notifier,
        owner=handle,
        keys={f"inbox:{handle}"},
        wait_s=wait,
        check=check,
        has_items=lambda listing: bool(listing.items),
    )
    return watched(page, status, waited_ms)
