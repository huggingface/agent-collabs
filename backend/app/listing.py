"""Shared list-query grammar (§16.2) over read-model records.

One grammar across ``GET /v1/messages``, ``/v1/results``, ``/v1/agents`` and
``/v1/inbox/{handle}``: filename-tier filters (``agent``, ``since``/``until``)
prune before any content is touched; frontmatter/content filters run over the
cached records; then order → cursor/limit → expand.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.errors import InvalidQuery
from app.models import MessageListing, MessageRecord
from app.naming import agent_from_filename
from app.read_model import Record
from app.validation import validate_agent_id


STAMP_LEN = len("YYYYMMDD-HHmmss-mmm")

_COMPACT_RE = re.compile(r"^\d{8}(?:-\d{6}(?:-\d{3})?)?$")

VERIFICATION_STATES = ("pending", "valid", "invalid")


def filename_stamp(filename: str) -> str:
    """The server-stamped chronological prefix of a message/result filename."""
    return filename[:STAMP_LEN]


def normalize_stamp(value: str, *, param: str) -> str:
    """Accept ISO 8601 or compact ``YYYYMMDD[-HHmmss[-mmm]]``; return a compact
    stamp comparable against the server-stamped filename prefix (UTC)."""
    v = value.strip()
    if _COMPACT_RE.match(v):
        if len(v) == 8:
            return v + "-000000-000"
        if len(v) == 15:
            return v + "-000"
        return v
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise InvalidQuery(
            f"`{param}` must be ISO 8601 or YYYYMMDD-HHmmss[-mmm], got {value!r}"
        )
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y%m%d-%H%M%S-") + f"{dt.microsecond // 1000:03d}"


def parse_verification_param(value: str | None) -> set[str] | None:
    """CSV of verification states, e.g. ``valid,pending``. None → no filter."""
    if value is None:
        return None
    states = {s.strip() for s in value.split(",") if s.strip()}
    bad = states - set(VERIFICATION_STATES)
    if bad or not states:
        raise InvalidQuery(
            f"`verification` must be a CSV of {VERIFICATION_STATES}, got {value!r}"
        )
    return states


def apply_filters(
    records: list[Record],
    *,
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    fm_eq: dict[str, str] | None = None,
    q: str | None = None,
) -> list[Record]:
    """``agent``/``since``/``until`` are answerable from filenames alone;
    ``fm_eq`` matches frontmatter values by string equality; ``q`` is a
    case-insensitive substring over frontmatter+body."""
    out: list[Record] = []
    for r in records:
        if agent is not None and agent_from_filename(r.filename) != agent:
            continue
        if since is not None and filename_stamp(r.filename) < since:
            continue
        if until is not None and filename_stamp(r.filename) > until:
            continue
        if fm_eq is not None:
            if any(str(r.frontmatter.get(k, "")) != want for k, want in fm_eq.items()):
                continue
        if q is not None and not _q_match(r, q):
            continue
        out.append(r)
    return out


def _q_match(r: Record, q: str) -> bool:
    ql = q.lower()
    if ql in r.body.lower():
        return True
    return any(ql in f"{k}: {v}".lower() for k, v in r.frontmatter.items())


def effective_limit(limit: int | None, expand: bool, cap: int) -> int | None:
    """Expanded pages are capped so one call can't serialize the whole corpus."""
    if not expand:
        return limit
    if limit is None or limit <= 0 or limit > cap:
        return cap
    return limit


def paginate(
    records: list[Record],
    *,
    order: str,
    limit: int | None,
    after: str | None,
    before: str | None,
) -> tuple[list[Record], str | None]:
    """Cursor + slice over filtered records (ascending filename order in).

    ``after``/``before`` are exclusive filename bounds. Returns the page and a
    ``next`` cursor (the page's last filename) when more matches remain in the
    traversal direction — pass it back as ``after`` for asc, ``before`` for
    desc.
    """
    if after is not None:
        records = [r for r in records if r.filename > after]
    if before is not None:
        records = [r for r in records if r.filename < before]
    ordered = list(reversed(records)) if order == "desc" else records
    if limit is not None and 0 < limit < len(ordered):
        page = ordered[:limit]
        return page, page[-1].filename
    return ordered, None


def list_message_like(
    records: list[Record],
    *,
    agent: str | None,
    since: str | None,
    until: str | None,
    type_: str | None,
    via: str | None,
    q: str | None,
    expand: bool,
    limit: int | None,
    order: str,
    after: str | None,
    before: str | None,
    expand_cap: int,
) -> MessageListing:
    """The full §16.2 pipeline for message-shaped folders (board and inboxes)."""
    if agent is not None:
        validate_agent_id(agent)
    fm_eq: dict[str, str] = {}
    if type_ is not None:
        fm_eq["type"] = type_
    if via is not None:
        fm_eq["via"] = via
    filtered = apply_filters(
        records,
        agent=agent,
        since=normalize_stamp(since, param="since") if since is not None else None,
        until=normalize_stamp(until, param="until") if until is not None else None,
        fm_eq=fm_eq or None,
        q=q,
    )
    page, next_cursor = paginate(
        filtered,
        order="desc" if order == "desc" else "asc",
        limit=effective_limit(limit, expand, expand_cap),
        after=after,
        before=before,
    )
    items: list[str] | list[MessageRecord]
    if expand:
        items = [
            MessageRecord(filename=r.filename, frontmatter=r.frontmatter, body=r.body)
            for r in page
        ]
    else:
        items = [r.filename for r in page]
    return MessageListing(
        count=len(records), matched=len(filtered), items=items, next=next_cursor
    )
