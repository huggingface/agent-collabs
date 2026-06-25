"""Trace & stats sharing (see TRACES_DESIGN.md).

`POST /v1/traces` promotes a session bundle from the agent's own scratch bucket,
exactly like `results`/`artifacts:sync`: identity is the bucket name (no token),
the manifest is read + stamped + indexed, and for `share=full` the native log is
hash-copied into the central bucket (bytes never stream through the Space) where
HF's built-in trace viewer renders it. `GET /v1/stats` is the project token
estimate; `GET /v1/traces[/{agent}/{session}]` is the library.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.audit import AuditLogger
from app.config import Settings
from app.deps import (
    get_audit,
    get_bucket_write_limiter,
    get_hub,
    get_read_model,
    get_settings_dep,
)
from app.errors import InvalidPath, NotFound, RateLimited, SyncTooLarge
from app.frontmatter import merge, parse, serialise
from app.hub import HubClient
from app.models import (
    StatsResponse,
    TraceListing,
    TracePostRequest,
    TracePostResponse,
    TraceRecord,
)
from app.naming import (
    TRACES_FOLDER,
    stamp_iso,
    stamp_yaml,
    trace_dir,
    utc_now,
)
from app.rate_limit import CompoundLimiter
from app.read_model import ReadModel
from app.routes.messages import require_registered
from app.trace_stats import (
    aggregate,
    completeness,
    list_traces,
    validate_trace_manifest,
)
from app.validation import resolve_source, validate_agent_id, validate_path_components


router = APIRouter()


@router.post("/v1/traces", response_model=TracePostResponse, status_code=201)
def post_trace(
    req: TracePostRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> TracePostResponse:
    parsed, agent_id = resolve_source(settings, req.source)
    if not parsed.path:
        raise InvalidPath(
            "source must point at your traces/<session>/ bundle dir",
            hint="write traces/<session>/manifest.md to your bucket, then pass that dir",
        )
    require_registered(read_model, hub, agent_id)

    allowed, retry = bucket_limiter.try_consume(parsed.bucket)
    if not allowed:
        raise RateLimited(retry)

    try:
        manifest_text = hub.read_text(parsed.join("manifest.md"))
    except FileNotFoundError:
        raise InvalidPath(
            "trace bundle is missing manifest.md",
            hint="share-trace writes manifest.md into traces/<session>/",
        )
    client_fm, body = parse(manifest_text)
    validate_trace_manifest(client_fm)
    session_id = str(client_fm["session_id"])
    validate_path_components(session_id)

    now = utc_now()
    comp = completeness(client_fm)
    server_fm = {
        "agent": agent_id,
        "promoted_at": stamp_yaml(now),
        "via": "bucket",
        "share": req.share,
        "completeness": comp,
    }
    merged = merge(client_fm, server_fm)
    content = serialise(merged, body)

    dest_dir = trace_dir(agent_id, session_id)
    manifest_dest = f"{dest_dir}/manifest.md"
    src_bucket = f"{parsed.org}/{parsed.bucket}"

    files_copied = 0
    bytes_copied = 0
    if req.share == "full":
        # Cap the bundle (same limits as artifacts:sync), then hash-copy the
        # whole tree — the stamped manifest below overwrites the copied one.
        listed = hub.list_bucket_dir(src_bucket, parsed.path)
        nbytes = sum(f.size for f in listed)
        if len(listed) > settings.sync_max_files:
            raise SyncTooLarge(
                f"{len(listed)} files exceeds cap of {settings.sync_max_files}"
            )
        if nbytes > settings.sync_max_bytes:
            raise SyncTooLarge(f"{nbytes} bytes exceeds cap of {settings.sync_max_bytes}")
        for _src, dest_path, size in hub.copy_tree_to_central(
            src_bucket, parsed.path, dest_dir
        ):
            if not dest_path.endswith("/manifest.md"):
                files_copied += 1
                bytes_copied += size

    hub.write_text_central(manifest_dest, content)
    read_model.write_through(
        manifest_dest, merged, body, len(content.encode("utf-8")), folder=TRACES_FOLDER
    )

    audit.write(
        agent_id=agent_id,
        route="/v1/traces",
        via="bucket",
        source=str(parsed),
        target_path=manifest_dest,
        bytes_count=len(content.encode("utf-8")) + bytes_copied,
        status_code=201,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={
            "share": req.share,
            "session_id": session_id,
            "files_copied": files_copied,
            "completeness": comp,
        },
    )

    return TracePostResponse(
        session_id=session_id,
        agent=agent_id,
        share=req.share,
        path=dest_dir + "/",
        files_copied=files_copied,
        bytes_copied=bytes_copied,
        completeness=comp,
    )


@router.get("/v1/stats", response_model=StatsResponse)
def get_stats(
    read_model: ReadModel = Depends(get_read_model),
) -> StatsResponse:
    """Project-wide token estimate — a REPORTED FLOOR (only shared sessions;
    null-token sessions excluded, surfaced as sessions_missing_tokens)."""
    return aggregate(read_model.records(TRACES_FOLDER), generated_at=stamp_iso(utc_now()))


@router.get("/v1/traces", response_model=TraceListing)
def list_traces_route(
    agent: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    share: str | None = None,
    q: str | None = None,
    expand: bool = False,
    limit: int | None = 10,
    order: str = "desc",
    after: str | None = None,
    before: str | None = None,
    settings: Settings = Depends(get_settings_dep),
    read_model: ReadModel = Depends(get_read_model),
) -> TraceListing:
    if agent is not None:
        validate_agent_id(agent)
    count, matched, items, nxt = list_traces(
        read_model.records(TRACES_FOLDER),
        agent=agent,
        harness=harness,
        model=model,
        share=share,
        q=q,
        expand=expand,
        limit=limit,
        order=order,
        after=after,
        before=before,
        expand_cap=settings.expand_max_limit,
    )
    return TraceListing(count=count, matched=matched, items=items, next=nxt)


@router.get("/v1/traces/{agent}/{session}", response_model=TraceRecord)
def get_trace(
    agent: str,
    session: str,
    read_model: ReadModel = Depends(get_read_model),
) -> TraceRecord:
    validate_agent_id(agent)
    validate_path_components(session)
    rec = read_model.record(TRACES_FOLDER, f"{agent}/{session}/manifest.md")
    if rec is None:
        raise NotFound(f"traces/{agent}/{session}")
    prefix = f"{TRACES_FOLDER}/{agent}/{session}/"
    log_files = sorted(
        e.rel_path
        for e in read_model.listing(TRACES_FOLDER)
        if e.rel_path.startswith(prefix) and not e.rel_path.endswith("/manifest.md")
    )
    return TraceRecord(
        agent=agent,
        session_id=session,
        frontmatter=rec.frontmatter,
        body=rec.body,
        path=prefix,
        log_files=log_files,
    )
