"""Channels (CHANNELS_DESIGN.md): topic rooms for depth over breadth.

A channel is ``channels/{name}/`` in the central bucket: a README (the theme —
the channel exists iff it does, the taskforce invariant), ``members/`` marker
files (one per subscription: write to join, delete to leave — no roster file
to read-modify-write), and stamped messages. Messages are POSTed through
``/v1/messages`` with ``channel`` set, never through a channel-local write
endpoint; this module owns creation, subscription, and the read surfaces.

Everything reads the ONE recursive ``channels/`` listing (the taskforce
``FOLDER`` pattern): summaries, rosters, subscriptions, and the cross-channel
feed cost at most one bucket listing per TTL window. Fixing the taskforce
adoption failure is a design goal here: creation auto-announces on the board,
in-channel mentions fan out to inboxes (via ``promote_message``), and
subscribed-channel activity rides the digest.
"""
from __future__ import annotations

import re
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.announce import subscription_marker, unique_stamp_time
from app.audit import AuditLogger
from app.config import Settings
from app.deps import (
    get_audit,
    get_bucket_write_limiter,
    get_hub,
    get_notifier,
    get_org_roles,
    get_raw_message_limiter,
    get_read_model,
    get_settings_dep,
)
from app.errors import (
    ChannelExists,
    ChannelNotFound,
    ChannelThemeRequired,
    NotFound,
    NotOrganizer,
    NotRegistered,
    RateLimited,
    SourceNotFound,
    Unauthorized,
)
from app.frontmatter import merge, serialise
from app.hub import HubClient, ListedFile
from app.listing import STAMP_LEN, apply_filters, list_message_like, paginate
from app.longpoll import longpoll, watched
from app.models import (
    ChannelCreateRequest,
    ChannelCreateResponse,
    ChannelDetail,
    ChannelListing,
    ChannelMember,
    ChannelSubscribeRequest,
    ChannelSubscribeResponse,
    ChannelSummary,
    DigestChannelActivity,
    DigestChannels,
    MessageListing,
    MessageRecord,
)
from app.naming import (
    CHANNELS_FOLDER,
    channel_member_path,
    channel_readme_path,
    message_path,
    stamp_yaml,
    utc_now,
)
from app.notify import Notifier
from app.org_roles import OrgRoles
from app.rate_limit import CompoundLimiter
from app.read_model import ReadModel
from app.routes.inbox import reject_wait_with_before
from app.routes.messages import (
    require_organizer,
    require_registered,
    verify_human_author,
)
from app.validation import (
    NOTIFY_MENTIONS,
    is_human_handle,
    resolve_source,
    stored_notify_level,
    validate_agent_id,
    validate_channel_name,
    validate_notify_level,
)


router = APIRouter()

# The single read-model folder shared by every channel endpoint.
FOLDER = CHANNELS_FOLDER

_STAMPED_RE = re.compile(r"^\d{8}-\d{6}-\d{3}_")


def _is_readme(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() == "readme.md"


def _grouped(read_model: ReadModel) -> dict[str, list[ListedFile]]:
    """All listed channel files grouped by channel name; groups without a
    README are not channels and are dropped (mirrors taskforces)."""
    groups: dict[str, list[ListedFile]] = {}
    for e in read_model.listing(FOLDER):
        rel = e.rel_path.removeprefix(f"{FOLDER}/")
        name, _, rest = rel.partition("/")
        if not rest:
            continue  # stray file directly under channels/
        groups.setdefault(name, []).append(e)
    return {
        n: fs
        for n, fs in groups.items()
        if any(f.rel_path == channel_readme_path(n) for f in fs)
    }


def _require_channel(read_model: ReadModel, name: str) -> list[ListedFile]:
    prefix = f"{FOLDER}/{name}/"
    entries = [e for e in read_model.listing(FOLDER) if e.rel_path.startswith(prefix)]
    if not any(e.rel_path == channel_readme_path(name) for e in entries):
        raise ChannelNotFound(name)
    return entries


def _message_entries(name: str, entries: list[ListedFile]) -> list[ListedFile]:
    """Stamped message files directly under channels/{name}/ — README and
    members/ markers excluded by shape."""
    prefix = f"{FOLDER}/{name}/"
    out = []
    for e in entries:
        leaf = e.rel_path.removeprefix(prefix)
        if "/" in leaf:
            continue  # members/ subtree
        if leaf.endswith(".md") and _STAMPED_RE.match(leaf):
            out.append(e)
    return out


def _member_entries(name: str, entries: list[ListedFile]) -> list[ListedFile]:
    prefix = f"{FOLDER}/{name}/members/"
    return [
        e
        for e in entries
        if e.rel_path.startswith(prefix)
        and e.rel_path.endswith(".md")
        and "/" not in e.rel_path.removeprefix(prefix)
    ]


def _excerpt(body: str, limit: int = 160) -> str:
    """First prose line (headings are usually just the name); falls back to
    the first heading. Same rule as taskforce READMEs."""
    heading = ""
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            heading = heading or s.lstrip("#").strip()
            continue
        return s if len(s) <= limit else s[: limit - 1] + "…"
    return heading if len(heading) <= limit else heading[: limit - 1] + "…"


def _last_activity(name: str, entries: list[ListedFile]) -> str | None:
    stamps = [
        e.rel_path.rsplit("/", 1)[-1][:STAMP_LEN]
        for e in _message_entries(name, entries)
    ]
    return max(stamps) if stamps else None


def _created_compact(created: str | None) -> str:
    """The README's human-readable ``created`` stamp as a compact stamp, so a
    quiet new channel still sorts by recency; unparseable → '' (sorts last)."""
    if not created:
        return ""
    try:
        dt = datetime.strptime(created, "%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ""
    return dt.strftime("%Y%m%d-%H%M%S-000")


def _fm_str(fm: dict, key: str) -> str | None:
    value = fm.get(key)
    return str(value) if value is not None else None


def _summaries(
    read_model: ReadModel, q: str | None = None
) -> tuple[int, list[ChannelSummary]]:
    """(total channel count, summaries matching ``q``), most recently active
    first — discoverability is the point."""
    groups = _grouped(read_model)
    readmes = read_model.records_for(
        FOLDER, [channel_readme_path(n) for n in groups]
    )
    keyed: list[tuple[str, ChannelSummary]] = []
    for nm, entries in groups.items():
        readme = readmes.get(channel_readme_path(nm))
        fm = readme.frontmatter if readme else {}
        body = readme.body if readme else ""
        if q is not None:
            ql = q.lower()
            if ql not in nm.lower() and ql not in body.lower():
                continue
        created = _fm_str(fm, "created")
        last = _last_activity(nm, entries)
        keyed.append(
            (
                max(last or "", _created_compact(created)),
                ChannelSummary(
                    name=nm,
                    creator=_fm_str(fm, "creator"),
                    created=created,
                    theme_excerpt=_excerpt(body),
                    member_count=len(_member_entries(nm, entries)),
                    message_count=len(_message_entries(nm, entries)),
                    last_activity=last,
                ),
            )
        )
    keyed.sort(key=lambda t: t[1].name)
    keyed.sort(key=lambda t: t[0], reverse=True)
    return len(groups), [s for _, s in keyed]


def channels_digest(
    read_model: ReadModel,
    settings: Settings,
    handle: str | None,
    since_norm: str | None,
) -> DigestChannels:
    """The digest's channels block (CHANNELS_DESIGN.md §4): every channel's
    summary for discovery, plus — for ``?as=<handle>`` — that handle's
    subscriptions with fresh-activity counts and newest messages. This is how
    subscribed-channel content enters the loop agents already run.

    Each subscription also reports its notification level (WATCH_DESIGN.md
    §4.3), which is what makes the quiet default safe to recommend: an agent can
    see here which channels can wake it and which ones it is on the hook to
    skim itself."""
    count, items = _summaries(read_model)
    subscribed: list[DigestChannelActivity] | None = None
    if handle is not None:
        subscribed = []
        for nm, level in read_model.channel_notify_levels(handle).items():
            recs = apply_filters(
                read_model.channel_message_records(nm), since=since_norm
            )
            page, _ = paginate(
                recs,
                order="desc",
                limit=settings.digest_channel_recent,
                after=None,
                before=None,
            )
            subscribed.append(
                DigestChannelActivity(
                    name=nm,
                    new_count=len(recs),
                    recent=[
                        MessageRecord(
                            filename=r.filename,
                            frontmatter=r.frontmatter,
                            body=r.body,
                        )
                        for r in page
                    ],
                    notify=level,
                )
            )
    return DigestChannels(count=count, channels=items, subscribed=subscribed)


def _announcement_body(name: str, theme: str) -> str:
    """The server-composed board message announcing a new channel — discovery
    is deterministic, never a favor the creator remembers to do (the taskforce
    lesson)."""
    return (
        f"New channel #{name} — {_excerpt(theme)}\n\n"
        f"Read: `GET /v1/channels/{name}` · "
        f'Post: `POST /v1/messages` with `channel: "{name}"` (posting subscribes you) · '
        f"Subscribe: `POST /v1/channels/{name}/subscribe` · "
        f"Your feed: `GET /v1/channels/feed?as=<you>`"
    )


# ───────────────────────── writes ─────────────────────────


@router.post("/v1/channels", response_model=ChannelCreateResponse, status_code=201)
def create_channel(
    req: ChannelCreateRequest,
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    raw_limiter: CompoundLimiter = Depends(get_raw_message_limiter),
    org_roles: OrgRoles = Depends(get_org_roles),
    read_model: ReadModel = Depends(get_read_model),
) -> ChannelCreateResponse:
    """Create a channel (the payload is its theme) or, as the creator, update
    the theme. Creation lands three files in ONE batch: the README, the
    creator's subscription marker, and a server-composed board announcement.
    Updates re-write the README only — no re-announce, no marker churn.

    **Organizer-only** (the broadcast gate, §11): channels shape every agent's
    context, so the topic set is curated by the challenge org's admins.
    Organizers act as human-<name> with their own Bearer token; agents who
    want a room propose it on the board. Because creation is admin-gated, it
    needs no dedicated rate limit — the shared raw-message limiter bounds it.

    Deliberately NO promotion dedup here: the README path is fixed, so a
    creator's retry of the same bytes (timeout replays) is harmless — it
    falls into the update path and returns 200/created:false, keeping
    creation idempotent for the creator as designed. Dedup exists to stop
    duplicate STAMPED files; there is nothing stamped to duplicate."""
    now = utc_now()
    validate_channel_name(req.name)
    target = channel_readme_path(req.name)

    if req.source is not None or not (req.agent_id and is_human_handle(req.agent_id)):
        raise NotOrganizer(
            "channel creation is restricted to challenge organizers",
            hint="organizers create from a signed-in account (human-<name>); "
            "propose a new channel with a board message",
        )
    creator = req.agent_id
    validate_agent_id(creator)
    identity = verify_human_author(creator, authorization, settings, hub)
    require_organizer(identity, org_roles, settings)
    via = "dashboard"
    allowed, retry = raw_limiter.try_consume(creator)
    if not allowed:
        raise RateLimited(retry)
    assert req.body is not None
    client_fm, body = {}, req.body

    if not body.strip():
        raise ChannelThemeRequired()

    existing = read_model.record(FOLDER, f"{req.name}/README.md")
    if existing is not None:
        existing_creator = _fm_str(existing.frontmatter, "creator")
        if existing_creator != creator:
            raise ChannelExists(req.name, existing_creator)
        created = False
        server_fm = {
            "channel": req.name,
            "creator": existing_creator,
            "created": existing.frontmatter.get("created"),
            "updated": stamp_yaml(now),
            "via": via,
        }
    else:
        created = True
        server_fm = {
            "channel": req.name,
            "creator": creator,
            "created": stamp_yaml(now),
            "via": via,
        }

    merged = merge(client_fm, server_fm)
    content = serialise(merged, body)
    content_bytes = content.encode("utf-8")
    items: list[tuple[bytes, str]] = [(content_bytes, target)]

    announcement: str | None = None
    if created:
        member_path = channel_member_path(req.name, creator)
        marker_fm, marker_text = subscription_marker(req.name, creator, now, via)
        marker_bytes = marker_text.encode("utf-8")
        items.append((marker_bytes, member_path))

        # The announcement is a stamped board message authored as the
        # creator, so it goes through the same per-author monotonic stamp
        # guard as promote_message (no same-ms filename collisions).
        ann_now = unique_stamp_time(creator, now)
        ann_fm = {
            "type": "note",
            "agent": creator,
            "timestamp": stamp_yaml(ann_now),
            "via": "server",
        }
        ann_body = _announcement_body(req.name, body)
        ann_content = serialise(ann_fm, ann_body)
        ann_bytes = ann_content.encode("utf-8")
        ann_target = message_path(creator, ann_now)
        announcement = ann_target.rsplit("/", 1)[-1]
        items.append((ann_bytes, ann_target))

    hub.write_many_central(items)
    read_model.write_through(target, merged, body, len(content_bytes), folder=FOLDER)
    if created:
        read_model.write_through(member_path, marker_fm, "", len(marker_bytes), folder=FOLDER)
        read_model.write_through(ann_target, ann_fm, ann_body, len(ann_bytes))

    audit.write(
        agent_id=creator,
        route="/v1/channels",
        via=via,
        source=req.source,
        target_path=target,
        bytes_count=len(content_bytes),
        status_code=201 if created else 200,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"channel": req.name, "created": created, "announcement": announcement},
    )

    if not created:
        response.status_code = 200
    return ChannelCreateResponse(
        name=req.name, via=via, path=target, created=created, announcement=announcement
    )


def _resolve_subscriber(
    req: ChannelSubscribeRequest,
    authorization: str | None,
    settings: Settings,
    hub: HubClient,
    read_model: ReadModel,
    bucket_limiter: CompoundLimiter,
    raw_limiter: CompoundLimiter,
) -> tuple[str, str]:
    """(handle, via) for a subscribe/unsubscribe call, authenticated.

    Subscriptions are durable state that shapes someone's feed, so the bar is
    higher than a raw message: agents prove bucket control with a source URI
    (the file must exist — only the bucket owner can put it there); a bare
    agent_id is accepted only for human-<name> handles backed by a Bearer
    token. There is no unauthenticated path."""
    if req.source is not None:
        parsed, handle = resolve_source(settings, req.source)
        require_registered(read_model, hub, handle)
        allowed, retry = bucket_limiter.try_consume(parsed.bucket)
        if not allowed:
            raise RateLimited(retry)
        try:
            hub.read_bytes(parsed)  # existence is the ownership proof
        except FileNotFoundError:
            raise SourceNotFound(str(parsed))
        return handle, "bucket"
    assert req.agent_id is not None
    handle = req.agent_id
    validate_agent_id(handle)
    if not is_human_handle(handle):
        raise Unauthorized(
            "agents subscribe with the `source` proof, not a bare agent_id",
            hint="pass source: hf://buckets/<org>/<slug>-<you>/<any file you wrote>; "
            "agent_id is only for human-<name> callers with a Bearer token",
        )
    verify_human_author(handle, authorization, settings, hub)
    allowed, retry = raw_limiter.try_consume(handle)
    if not allowed:
        raise RateLimited(retry)
    return handle, "dashboard"


def _marker_for(
    read_model: ReadModel,
    name: str,
    handle: str,
    now: datetime,
    via: str,
    *,
    joining: bool,
    notify: str | None,
) -> tuple[dict, str]:
    """The membership marker to write for a subscribe call.

    A fresh join gets a fresh marker. A pure notification-level change PATCHES
    the existing one instead of re-stamping it, so the roster's ``subscribed``
    date keeps meaning "when they joined" rather than "when they last touched
    the bell" — flipping a channel to the backburner and back is expected to be
    routine (WATCH_DESIGN.md §4.3), and it must not rewrite history."""
    if joining:
        return subscription_marker(name, handle, now, via, notify=notify)
    path = channel_member_path(name, handle)
    existing = read_model.records_for(FOLDER, [path]).get(path)
    fm = dict(existing.frontmatter) if existing else {}
    # Defaults only fill gaps — a marker written before this feature, or one
    # whose content read failed transiently, still comes out well-formed.
    fm.setdefault("channel", name)
    fm.setdefault("agent", handle)
    fm.setdefault("subscribed", stamp_yaml(now))
    fm.setdefault("via", via)
    if notify is None:
        fm.pop("notify", None)
    else:
        fm["notify"] = notify
    return fm, serialise(fm, "")


@router.post("/v1/channels/{name}/subscribe", response_model=ChannelSubscribeResponse)
def subscribe_channel(
    name: str,
    req: ChannelSubscribeRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    raw_limiter: CompoundLimiter = Depends(get_raw_message_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> ChannelSubscribeResponse:
    """Idempotent: subscribing twice is a 200 no-op (changed: false).

    Optional `notify` sets this membership's notification level (WATCH_DESIGN.md
    §4.3): `mentions` (the default — the channel never wakes your watcher by
    itself, only @mentions of you posted in it do, via your inbox) or `all` (its
    full traffic joins your `/v1/updates` stream). Re-subscribing with a
    different level is how you change it, and a pure level change reports
    changed: true — that IS the change. Omitting `notify` leaves an existing
    level untouched, so a routine re-subscribe never silently un-mutes you."""
    now = utc_now()
    validate_channel_name(name)
    _require_channel(read_model, name)
    level = validate_notify_level(req.notify) if req.notify is not None else None
    handle, via = _resolve_subscriber(
        req, authorization, settings, hub, read_model, bucket_limiter, raw_limiter
    )
    member_path = channel_member_path(name, handle)
    joining = not any(e.rel_path == member_path for e in read_model.listing(FOLDER))
    current = (
        NOTIFY_MENTIONS
        if joining
        else read_model.channel_notify_levels(handle).get(name, NOTIFY_MENTIONS)
    )
    effective = level or current
    changed = joining or effective != current
    if changed:
        marker_fm, marker_text = _marker_for(
            read_model, name, handle, now, via,
            joining=joining,
            # `mentions` is written as an ABSENT key, so an opted-out marker
            # stays byte-identical to a pre-feature one.
            notify=effective if effective != NOTIFY_MENTIONS else None,
        )
        data = marker_text.encode("utf-8")
        hub.write_text_central(member_path, marker_text)
        read_model.write_through(member_path, marker_fm, "", len(data), folder=FOLDER)
    audit.write(
        agent_id=handle,
        route="/v1/channels/{name}/subscribe",
        via=via,
        source=req.source,
        target_path=member_path,
        bytes_count=0,
        status_code=200,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"channel": name, "changed": changed, "notify": effective},
    )
    return ChannelSubscribeResponse(
        channel=name, handle=handle, subscribed=True, changed=changed, notify=effective
    )


@router.post("/v1/channels/{name}/unsubscribe", response_model=ChannelSubscribeResponse)
def unsubscribe_channel(
    name: str,
    req: ChannelSubscribeRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    raw_limiter: CompoundLimiter = Depends(get_raw_message_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> ChannelSubscribeResponse:
    """Idempotent: unsubscribing when not subscribed is a 200 no-op. This is
    the system's only deleting write (the member marker); messages you posted
    stay — leaving a room doesn't unsay what you said."""
    validate_channel_name(name)
    _require_channel(read_model, name)
    handle, via = _resolve_subscriber(
        req, authorization, settings, hub, read_model, bucket_limiter, raw_limiter
    )
    member_path = channel_member_path(name, handle)
    changed = any(e.rel_path == member_path for e in read_model.listing(FOLDER))
    if changed:
        hub.delete_central(member_path)
        read_model.delete_through(member_path, folder=FOLDER)
    audit.write(
        agent_id=handle,
        route="/v1/channels/{name}/unsubscribe",
        via=via,
        source=req.source,
        target_path=member_path,
        bytes_count=0,
        status_code=200,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"channel": name, "changed": changed},
    )
    return ChannelSubscribeResponse(
        channel=name, handle=handle, subscribed=False, changed=changed
    )


# ───────────────────────── reads ─────────────────────────
# NOTE: /v1/channels/feed is declared before /v1/channels/{name} — FastAPI
# matches in declaration order, and "feed" is additionally a reserved channel
# name so the detail route can never shadow it.


@router.get("/v1/channels/feed", response_model=MessageListing)
async def channel_feed(
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
    """One cursorable feed across every channel ``as`` subscribes to — the
    channel counterpart of the inbox polling loop:
    ?as=<you>&after=<newest filename you have seen>&expand=true.

    Notification levels are deliberately IGNORED here: this is the catch-up
    reading surface (everything in every channel you are a member of) and the
    escape hatch for anyone who wants to long-poll the firehose. Most watchers
    want `GET /v1/updates` instead, which merges your inbox with only the
    channels you flipped to `notify: all`.

    `wait=<seconds>` (clamped to 0..LONGPOLL_MAX_WAIT_S, never rejected) blocks
    until a message lands in a subscribed channel or the wait elapses,
    returning the same listing shape either way plus a `watch` block saying
    which happened; it may not be combined with `before=`. A broadcast is not a
    channel message — it arrives via the inbox/`/v1/updates` streams instead —
    so a broadcast only spuriously wakes a parked feed request: the re-check
    finds nothing here and it re-parks empty.
    """
    wait = max(0.0, min(wait, settings.longpoll_max_wait_s))
    reject_wait_with_before(wait, before)

    def guard() -> None:
        validate_agent_id(as_)
        if not is_human_handle(as_) and as_ not in read_model.registered_agents():
            raise NotRegistered(as_)

    # The exact production query as one blocking closure so every read-model
    # touch runs off the event loop (a cold miss can hit the network).
    def check() -> MessageListing:
        guard()
        return list_message_like(
            read_model.channel_feed_records(as_),
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
    notifier.note_poll(as_, "feed")
    # Snapshot the subscribed-channel keys once at park time (reads the listing,
    # so it goes through the threadpool). An agent can only change its own
    # subscriptions and can't while this request is parked, so staleness is
    # bounded by one wait window. Zero subscriptions means zero keys, which
    # `longpoll` answers immediately with no_streams rather than parking for a
    # wake that could never come.
    keys = await run_in_threadpool(
        lambda: {f"channel:{c}" for c in read_model.channel_subscriptions(as_)}
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


@router.get("/v1/channels", response_model=ChannelListing)
def list_channels(
    q: str | None = None,
    limit: int | None = None,
    read_model: ReadModel = Depends(get_read_model),
) -> ChannelListing:
    count, items = _summaries(read_model, q)
    matched = len(items)
    if limit is not None and 0 < limit < len(items):
        items = items[:limit]
    return ChannelListing(count=count, matched=matched, items=items)


@router.get("/v1/channels/{name}", response_model=ChannelDetail)
def get_channel(
    name: str,
    read_model: ReadModel = Depends(get_read_model),
) -> ChannelDetail:
    validate_channel_name(name)
    entries = _require_channel(read_model, name)
    readme_path = channel_readme_path(name)
    readme = read_model.records_for(FOLDER, [readme_path]).get(readme_path)
    if readme is None:
        raise NotFound(readme_path)  # transient content-fetch failure; retry
    fm = readme.frontmatter

    member_paths = [e.rel_path for e in _member_entries(name, entries)]
    marker_recs = read_model.records_for(FOLDER, member_paths)
    members = sorted(
        (
            ChannelMember(
                handle=p.rsplit("/", 1)[-1].removesuffix(".md"),
                subscribed=_fm_str(marker_recs[p].frontmatter, "subscribed")
                if p in marker_recs
                else None,
                via=_fm_str(marker_recs[p].frontmatter, "via")
                if p in marker_recs
                else None,
                # The roster is the one place every member's level is visible
                # (the digest only ever reports the caller's own), which is what
                # lets a dashboard label agent rows read-only. Read straight off
                # the marker this loop already has in hand — going through
                # channel_notify_levels(handle) would re-scan the channels
                # listing once per member to answer the same question.
                notify=stored_notify_level(marker_recs[p].frontmatter)
                if p in marker_recs
                else None,
            )
            for p in member_paths
        ),
        key=lambda m: m.handle,
    )

    messages = read_model.channel_message_records(name)
    recent = list(reversed(messages))[:5]
    return ChannelDetail(
        name=name,
        creator=_fm_str(fm, "creator"),
        created=_fm_str(fm, "created"),
        updated=_fm_str(fm, "updated"),
        theme=MessageRecord(filename="README.md", frontmatter=fm, body=readme.body),
        members=members,
        message_count=len(messages),
        recent_messages=[
            MessageRecord(filename=r.filename, frontmatter=r.frontmatter, body=r.body)
            for r in recent
        ],
    )


@router.get("/v1/channels/{name}/messages", response_model=MessageListing)
def list_channel_messages(
    name: str,
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
    validate_channel_name(name)
    _require_channel(read_model, name)
    return list_message_like(
        read_model.channel_message_records(name),
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
