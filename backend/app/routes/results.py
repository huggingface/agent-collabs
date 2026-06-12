from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.audit import AuditLogger
from app.config import Settings
from app.dedup import PromotionLRU, content_hash
from app.deps import (
    get_audit,
    get_bucket_write_limiter,
    get_dedup,
    get_hub,
    get_read_model,
    get_settings_dep,
    get_verification_status,
    get_verifier,
)
from app.errors import AlreadyPromoted, NotFound, RateLimited
from app.frontmatter import merge, parse, serialise, validate_result_frontmatter
from app.hub import HubClient
from app.listing import (
    apply_filters,
    effective_limit,
    normalize_stamp,
    paginate,
    parse_verification_param,
)
from app.models import ResultListing, ResultPostRequest, ResultRecord, ResultResponse
from app.naming import result_path, stamp_yaml, utc_now
from app.rate_limit import CompoundLimiter
from app.read_model import ReadModel
from app.routes.messages import require_registered
from app.validation import resolve_source, validate_agent_id
from app.verification import PENDING, VerificationStatusStore
from app.verifier import Verifier


router = APIRouter()


@router.post("/v1/results", response_model=ResultResponse, status_code=201)
def post_result(
    req: ResultPostRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    dedup: PromotionLRU = Depends(get_dedup),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    verification: VerificationStatusStore = Depends(get_verification_status),
    read_model: ReadModel = Depends(get_read_model),
    verifier: Verifier = Depends(get_verifier),
) -> ResultResponse:
    parsed, agent_id = resolve_source(settings, req.source)
    require_registered(read_model, hub, agent_id)

    allowed, retry = bucket_limiter.try_consume(parsed.bucket)
    if not allowed:
        raise RateLimited(retry)

    body_bytes = hub.read_bytes(parsed)
    body_text = body_bytes.decode("utf-8")
    client_fm, source_body = parse(body_text)
    validate_result_frontmatter(settings, client_fm)

    dest_folder = "results"
    existing = dedup.get(content_hash(body_bytes), dest_folder)
    if existing:
        raise AlreadyPromoted(existing)

    now = utc_now()
    server_fm = {
        "agent": agent_id,
        "timestamp": stamp_yaml(now),
        "via": "bucket",
    }
    merged = merge(client_fm, server_fm)
    content = serialise(merged, source_body)

    target = result_path(agent_id, now)
    hub.write_text_central(target, content)
    read_model.write_through(target, merged, source_body, len(content.encode("utf-8")))
    filename = target.rsplit("/", 1)[-1]
    dedup.record(content_hash(body_bytes), dest_folder, filename)
    # Track the freshly promoted result as `pending` in the verification index.
    # Best-effort: the result is already written, so a failure here must not 500.
    verification.mark_pending(filename)
    # If this claims to beat the verified champion, re-run it on the private
    # set. Best-effort and async — the POST never fails or waits on it.
    verifier.maybe_trigger(filename, merged)

    audit.write(
        agent_id=agent_id,
        route="/v1/results",
        via="bucket",
        source=str(parsed),
        target_path=target,
        bytes_count=len(content.encode("utf-8")),
        status_code=201,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )

    return ResultResponse(filename=filename, via="bucket", path=target)


@router.get("/v1/results", response_model=ResultListing)
def list_results(
    agent: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    verification: str | None = None,
    q: str | None = None,
    expand: bool = False,
    limit: int | None = 10,
    order: str = "desc",
    after: str | None = None,
    before: str | None = None,
    settings: Settings = Depends(get_settings_dep),
    read_model: ReadModel = Depends(get_read_model),
) -> ResultListing:
    if agent is not None:
        validate_agent_id(agent)
    states = parse_verification_param(verification)
    records = read_model.records("results")
    index = read_model.verification_index()

    filtered = apply_filters(
        records,
        agent=agent,
        since=normalize_stamp(since, param="since") if since is not None else None,
        until=normalize_stamp(until, param="until") if until is not None else None,
        fm_eq={"status": status} if status is not None else None,
        q=q,
    )
    if states is not None:
        filtered = [r for r in filtered if index.get(r.filename, PENDING) in states]

    page, next_cursor = paginate(
        filtered,
        order="desc" if order == "desc" else "asc",
        limit=effective_limit(limit, expand, settings.expand_max_limit),
        after=after,
        before=before,
    )
    items: list[str] | list[ResultRecord]
    if expand:
        items = [
            ResultRecord(
                filename=r.filename,
                frontmatter=r.frontmatter,
                body=r.body,
                verification=index.get(r.filename, PENDING),
            )
            for r in page
        ]
    else:
        items = [r.filename for r in page]
    return ResultListing(
        count=len(records), matched=len(filtered), items=items, next=next_cursor
    )


@router.get("/v1/results/{filename}", response_model=ResultRecord)
def get_result(
    filename: str,
    read_model: ReadModel = Depends(get_read_model),
) -> ResultRecord:
    rec = read_model.record("results", filename)
    if rec is None:
        raise NotFound(f"results/{filename}")
    return ResultRecord(
        filename=rec.filename,
        frontmatter=rec.frontmatter,
        body=rec.body,
        verification=read_model.verification_index().get(rec.filename, PENDING),
    )
