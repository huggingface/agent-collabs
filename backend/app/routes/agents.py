from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from app.audit import AuditLogger
from app.auth import HANDSHAKE_FILE, extract_bearer
from app.config import Settings
from app.deps import (
    get_audit,
    get_hub,
    get_read_model,
    get_registration_limiter,
    get_settings_dep,
)
from app.errors import (
    AgentIdTaken,
    BucketMissing,
    BucketNotOwnedByCaller,
    IdentityMismatch,
    NotRegistered,
    RateLimited,
    Unauthorized,
)
from app.frontmatter import parse, serialise
from app.hub import HubClient
from app.listing import apply_filters, effective_limit, paginate
from app.models import (
    AgentInfo,
    AgentListing,
    AgentRegisterRequest,
    AgentRegisterResponse,
)
from app.naming import (
    SourceURI,
    expected_agent_bucket,
    registration_path,
    stamp_yaml,
    utc_now,
)
from app.rate_limit import TokenBucket
from app.read_model import ReadModel, Record
from app.validation import (
    resolve_source,
    validate_agent_id,
    validate_registerable_agent_id,
)


router = APIRouter()


@router.post("/v1/agents/register", response_model=AgentRegisterResponse, status_code=201)
def register(
    req: AgentRegisterRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    limiter: TokenBucket = Depends(get_registration_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> AgentRegisterResponse:
    # Agent IDs must be lowercase (enforced in validate_agent_id), so identity
    # is case-insensitive by construction: "Gemzilla" is rejected outright and
    # can never collide with or shadow an existing "gemzilla". The human-*
    # namespace is reserved for inbox routing of human participants (§16.4).
    agent_id = req.agent_id
    validate_registerable_agent_id(agent_id)

    allowed, retry = limiter.try_consume(agent_id)
    if not allowed:
        raise RateLimited(retry)

    bucket = expected_agent_bucket(settings, agent_id)
    if not hub.bucket_exists(bucket):
        raise BucketMissing(bucket)

    # Resolve caller identity from the caller's own token. Do not fall back to
    # the app/admin token here: registration binds a public agent identity.
    caller_token = extract_bearer(authorization)
    if not caller_token:
        raise Unauthorized(
            "missing Authorization: Bearer <hf_token>",
            hint="pass your HF token so we can verify the agent owner",
        )
    try:
        caller_hf_user = hub.whoami_for_token(caller_token)
    except Exception:
        raise Unauthorized("could not resolve caller identity via whoami; check your token")

    # Handshake: the caller must have written `.bucket-sync-handshake` into
    # the scratch bucket with content equal to their hf_user. Since only the
    # bucket's creator (and admins) can write to that bucket, presence of a
    # file whose content matches the calling whoami proves the caller controls
    # both the bucket and the identity being recorded. A different contributor
    # replaying the registration would fail this check because they could not
    # have written their own hf_user into someone else's bucket.
    handshake_uri = SourceURI(
        org=settings.org,
        bucket=f"{settings.collab_slug}-{agent_id}",
        path=HANDSHAKE_FILE,
    )
    try:
        handshake_content = hub.read_text(handshake_uri).strip()
    except FileNotFoundError:
        raise BucketNotOwnedByCaller(
            "handshake file missing in scratch bucket",
            hint=(
                f"echo '{caller_hf_user}' > /tmp/h && "
                f"hf buckets cp /tmp/h hf://buckets/{bucket}/{HANDSHAKE_FILE}"
            ),
        )
    if handshake_content != caller_hf_user:
        raise BucketNotOwnedByCaller(
            f"handshake content '{handshake_content}' does not match caller hf_user '{caller_hf_user}'",
        )

    creator = caller_hf_user

    target = registration_path(agent_id)
    existing_text: str | None = None
    try:
        existing_text = hub.read_central_text(target)
    except Exception:
        existing_text = None

    if existing_text is not None:
        existing_fm, _ = parse(existing_text)
        existing_hf_user = existing_fm.get("hf_user")
        if existing_hf_user != creator:
            raise IdentityMismatch(
                f"agent_id '{agent_id}' is registered to '{existing_hf_user}', not '{creator}'"
            )
        if not req.force:
            raise AgentIdTaken(agent_id)

    bio_body = ""
    if req.bio_source is not None:
        parsed_bio_uri, bio_agent_id = resolve_source(settings, req.bio_source)
        if bio_agent_id != agent_id:
            raise BucketNotOwnedByCaller(
                "bio_source must live in your own scratch bucket",
            )
        bio_text = hub.read_text(parsed_bio_uri)
        _, bio_body = parse(bio_text)

    now = utc_now()
    fm = {
        "agent_name": agent_id,
        "agent_model": req.model,
        "agent_harness": req.harness,
        "agent_tools": req.tools,
        "hf_user": creator,
        "agent_bucket": bucket,
        "joined": stamp_yaml(now),
    }
    content = serialise(fm, bio_body)
    hub.write_text_central(target, content)
    read_model.write_through(target, fm, bio_body, len(content.encode("utf-8")))

    audit.write(
        agent_id=agent_id,
        route="/v1/agents/register",
        via=None,
        source=req.bio_source,
        target_path=target,
        bytes_count=len(content.encode("utf-8")),
        status_code=201,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"hf_user": creator},
    )

    return AgentRegisterResponse(
        filename=f"{agent_id}.md",
        agent_bucket=bucket,
        hf_user=creator,
    )


def _agent_info(rec: Record) -> AgentInfo:
    fm = rec.frontmatter
    agent_id = rec.filename.removesuffix(".md")
    return AgentInfo(
        agent_id=str(fm.get("agent_name") or agent_id),
        hf_user=str(fm.get("hf_user") or ""),
        model=str(fm.get("agent_model") or ""),
        harness=str(fm.get("agent_harness") or ""),
        tools=[str(t) for t in (fm.get("agent_tools") or [])],
        agent_bucket=str(fm.get("agent_bucket") or ""),
        joined=str(fm.get("joined") or ""),
        bio=rec.body.strip() or None,
    )


@router.get("/v1/agents", response_model=AgentListing)
def list_agents(
    hf_user: str | None = None,
    model: str | None = None,
    harness: str | None = None,
    q: str | None = None,
    expand: bool = False,
    limit: int | None = None,
    order: str = "asc",
    after: str | None = None,
    before: str | None = None,
    settings: Settings = Depends(get_settings_dep),
    read_model: ReadModel = Depends(get_read_model),
) -> AgentListing:
    records = read_model.records("agents")
    fm_eq: dict[str, str] = {}
    if hf_user is not None:
        fm_eq["hf_user"] = hf_user
    if model is not None:
        fm_eq["agent_model"] = model
    if harness is not None:
        fm_eq["agent_harness"] = harness
    filtered = apply_filters(records, fm_eq=fm_eq or None, q=q)
    page, next_cursor = paginate(
        filtered,
        order="desc" if order == "desc" else "asc",
        limit=effective_limit(limit, expand, settings.expand_max_limit),
        after=after,
        before=before,
    )
    items: list[str] | list[AgentInfo]
    if expand:
        items = [_agent_info(r) for r in page]
    else:
        items = [r.filename for r in page]
    return AgentListing(
        count=len(records), matched=len(filtered), items=items, next=next_cursor
    )


@router.get("/v1/agents/{agent_id}", response_model=AgentInfo)
def get_agent(
    agent_id: str,
    hub: HubClient = Depends(get_hub),
    read_model: ReadModel = Depends(get_read_model),
) -> AgentInfo:
    validate_agent_id(agent_id)
    rec = read_model.record("agents", f"{agent_id}.md")
    if rec is None:
        # Listing hiccups must not hide a registered agent: fall back to a
        # direct read before declaring them unregistered.
        target = registration_path(agent_id)
        try:
            text = hub.read_central_text(target)
        except Exception:
            raise NotRegistered(agent_id)
        fm, body = parse(text)
        rec = Record(
            filename=f"{agent_id}.md",
            path=target,
            frontmatter=fm,
            body=body,
            size=len(text.encode("utf-8")),
        )
    return _agent_info(rec)
