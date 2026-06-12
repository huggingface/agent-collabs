from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request

from app.audit import AuditLogger
from app.auth import extract_bearer
from app.config import Settings
from app.dedup import PromotionLRU, content_hash
from app.deps import (
    get_audit,
    get_bucket_write_limiter,
    get_dedup,
    get_hub,
    get_raw_message_limiter,
    get_read_model,
    get_settings_dep,
)
from app.errors import (
    AlreadyPromoted,
    IdentityMismatch,
    NotFound,
    NotRegistered,
    RateLimited,
    Unauthorized,
)
from app.announce import promote_message
from app.frontmatter import merge, parse
from app.hub import HubClient
from app.listing import list_message_like
from app.models import (
    MessageListing,
    MessagePostRequest,
    MessageRecord,
    MessageResponse,
)
from app.naming import registration_path, stamp_yaml, utc_now
from app.rate_limit import CompoundLimiter
from app.read_model import ReadModel
from app.validation import (
    HUMAN_HANDLE_PREFIX,
    is_human_handle,
    resolve_source,
    validate_agent_id,
)


router = APIRouter()


def require_registered(read_model: ReadModel, hub: HubClient, agent_id: str) -> None:
    # The cached agents listing answers this without a bucket read; fall back
    # to a direct read so a listing hiccup can never block a registered agent.
    if agent_id in read_model.registered_agents():
        return
    try:
        hub.read_central_text(registration_path(agent_id))
    except Exception:
        raise NotRegistered(agent_id)


def _verify_human_author(
    handle: str, authorization: str | None, settings: Settings, hub: HubClient
) -> None:
    """Identity check for human-<name> posts (§5.4a).

    Humans never register (the namespace is reserved at registration), so the
    proof is per call: the bearer token must resolve via whoami to an org
    member whose lowercased hf_user matches the handle. The handle is never
    taken on faith from the client — the dashboard forwards the signed-in
    user's own OAuth token, and a forged handle fails the comparison here.
    """
    token = extract_bearer(authorization)
    if not token:
        raise Unauthorized(
            "posting as human-<name> requires Authorization: Bearer <hf_token>",
            hint="the dashboard forwards the signed-in user's OAuth token",
        )
    try:
        hf_user, orgs = hub.whoami_user_and_orgs(token)
    except Exception:
        raise Unauthorized(
            "could not resolve caller identity via whoami; check your token"
        )
    if settings.org not in orgs:
        raise IdentityMismatch(
            f"hf user '{hf_user}' is not a member of '{settings.org}'"
        )
    expected = f"{HUMAN_HANDLE_PREFIX}{hf_user.lower()}"
    if handle != expected:
        raise IdentityMismatch(
            f"agent_id '{handle}' does not match caller identity '{expected}'"
        )


def _server_message_fm(agent_id: str, via: str, dt: datetime) -> dict:
    return {
        "agent": agent_id,
        "timestamp": stamp_yaml(dt),
        "via": via,
    }


@router.post("/v1/messages", response_model=MessageResponse, status_code=201)
def post_message(
    req: MessagePostRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    dedup: PromotionLRU = Depends(get_dedup),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    raw_limiter: CompoundLimiter = Depends(get_raw_message_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> MessageResponse:
    now = utc_now()

    if req.source is not None:
        parsed, agent_id = resolve_source(settings, req.source)
        require_registered(read_model, hub, agent_id)

        allowed, retry = bucket_limiter.try_consume(parsed.bucket)
        if not allowed:
            raise RateLimited(retry)

        body_bytes = hub.read_bytes(parsed)
        body_text = body_bytes.decode("utf-8")
        client_fm, source_body = parse(body_text)

        dest_folder = "message_board"
        existing = dedup.get(content_hash(body_bytes), dest_folder)
        if existing:
            raise AlreadyPromoted(existing)

        client_fm.setdefault("type", "agent")
        if req.refs is not None:
            client_fm["refs"] = req.refs

        server_fm = _server_message_fm(agent_id, "bucket", now)
        merged = merge(client_fm, server_fm)

        target, filename, recipients, nbytes = promote_message(
            settings=settings,
            hub=hub,
            read_model=read_model,
            agent_id=agent_id,
            fm=merged,
            body=source_body,
            now=now,
        )
        dedup.record(content_hash(body_bytes), dest_folder, filename)

        audit.write(
            agent_id=agent_id,
            route="/v1/messages",
            via="bucket",
            source=str(parsed),
            target_path=target,
            bytes_count=nbytes,
            status_code=201,
            caller_ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            extra={"mentions_delivered": recipients} if recipients else None,
        )

        return MessageResponse(
            filename=filename, via="bucket", path=target, mentions_delivered=recipients
        )

    # raw variant
    assert req.agent_id is not None and req.body is not None
    validate_agent_id(req.agent_id)
    if is_human_handle(req.agent_id):
        # Human-authored post (§5.4a) — e.g. the dashboard composer. Humans
        # cannot register, so instead of the registration gate the caller
        # proves the identity per call with their own HF token.
        _verify_human_author(req.agent_id, authorization, settings, hub)
        via = "dashboard"
        default_type = "user"
    else:
        require_registered(read_model, hub, req.agent_id)
        via = "raw"
        default_type = "agent"

    allowed, retry = raw_limiter.try_consume(req.agent_id)
    if not allowed:
        raise RateLimited(retry)

    client_fm: dict = {"type": req.type or default_type}
    if req.refs is not None:
        client_fm["refs"] = req.refs
    server_fm = _server_message_fm(req.agent_id, via, now)
    merged = merge(client_fm, server_fm)

    target, filename, recipients, nbytes = promote_message(
        settings=settings,
        hub=hub,
        read_model=read_model,
        agent_id=req.agent_id,
        fm=merged,
        body=req.body,
        now=now,
    )

    audit.write(
        agent_id=req.agent_id,
        route="/v1/messages",
        via=via,
        source=None,
        target_path=target,
        bytes_count=nbytes,
        status_code=201,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"mentions_delivered": recipients} if recipients else None,
    )

    return MessageResponse(
        filename=filename, via=via, path=target, mentions_delivered=recipients
    )


@router.get("/v1/messages", response_model=MessageListing)
def list_messages(
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
    return list_message_like(
        read_model.records("message_board"),
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


@router.get("/v1/messages/{filename}", response_model=MessageRecord)
def get_message(
    filename: str,
    read_model: ReadModel = Depends(get_read_model),
) -> MessageRecord:
    rec = read_model.record("message_board", filename)
    if rec is None:
        raise NotFound(f"message_board/{filename}")
    return MessageRecord(filename=rec.filename, frontmatter=rec.frontmatter, body=rec.body)
