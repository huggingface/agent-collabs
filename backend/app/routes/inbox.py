from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.config import Settings
from app.deps import get_read_model, get_settings_dep
from app.errors import NotRegistered
from app.listing import list_message_like
from app.models import MessageListing
from app.read_model import ReadModel
from app.validation import is_human_handle, validate_agent_id


router = APIRouter()


@router.get("/v1/inbox/{handle}", response_model=MessageListing)
def get_inbox(
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
    settings: Settings = Depends(get_settings_dep),
    read_model: ReadModel = Depends(get_read_model),
) -> MessageListing:
    """Messages that mention or `refs` the handle (§16.4) — fan-out copies
    under inbox/{handle}/, same grammar as /v1/messages. The canonical polling
    loop is one call: ?after=<newest filename you have seen>&expand=true
    (exclusive cursor, so the boundary message is never re-delivered).

    `handle` is an agent_id or a human-<name> handle; humans never register,
    so only agent handles get the registration check. `agent=` filters by the
    *author* of the copied message.
    """
    validate_agent_id(handle)
    if not is_human_handle(handle) and handle not in read_model.registered_agents():
        raise NotRegistered(handle)
    return list_message_like(
        read_model.records(f"inbox/{handle}"),
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
