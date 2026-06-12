from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.audit import AuditLogger
from app.config import Settings
from app.deps import (
    get_audit,
    get_bucket_write_limiter,
    get_hub,
    get_settings_dep,
)
from app.errors import NotRegistered, RateLimited, SyncTooLarge
from app.hub import HubClient
from app.models import (
    ArtifactSyncRequest,
    SharedResourceSyncRequest,
    SyncFile,
    SyncResponse,
)
from app.naming import artifact_dest_dir, registration_path
from app.rate_limit import CompoundLimiter
from app.validation import (
    check_dest_not_blocked,
    resolve_source,
    validate_shared_dest_path,
    validate_slug,
)


router = APIRouter()


def _require_registered(hub: HubClient, agent_id: str) -> None:
    try:
        hub.read_central_text(registration_path(agent_id))
    except Exception:
        raise NotRegistered(agent_id)


def _check_sync_caps(settings: Settings, files: list) -> int:
    total_bytes = sum(f.size for f in files)
    if len(files) > settings.sync_max_files:
        raise SyncTooLarge(f"{len(files)} files exceeds cap of {settings.sync_max_files}")
    if total_bytes > settings.sync_max_bytes:
        raise SyncTooLarge(
            f"{total_bytes} bytes exceeds cap of {settings.sync_max_bytes}"
        )
    return total_bytes


def _execute_sync(
    hub: HubClient,
    src_bucket: str,
    src_prefix: str,
    dest_prefix: str,
) -> list[SyncFile]:
    out: list[SyncFile] = []
    for src_path, dest_path, size in hub.copy_tree_to_central(src_bucket, src_prefix, dest_prefix):
        out.append(SyncFile(src_path=src_path, dest_path=dest_path, bytes=size))
    return out


@router.post("/v1/artifacts:sync", response_model=SyncResponse)
def artifacts_sync(
    req: ArtifactSyncRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
) -> SyncResponse:
    validate_slug(req.dest_slug)
    parsed, agent_id = resolve_source(settings, req.source)
    _require_registered(hub, agent_id)

    allowed, retry = limiter.try_consume(parsed.bucket)
    if not allowed:
        raise RateLimited(retry)

    src_bucket = f"{parsed.org}/{parsed.bucket}"
    src_prefix = parsed.path

    files = hub.list_bucket_dir(src_bucket, src_prefix)
    _check_sync_caps(settings, files)

    dest_prefix = artifact_dest_dir(req.dest_slug, agent_id)
    check_dest_not_blocked(dest_prefix)

    copied = _execute_sync(hub, src_bucket, src_prefix, dest_prefix)
    total = sum(f.bytes for f in copied)

    audit.write(
        agent_id=agent_id,
        route="/v1/artifacts:sync",
        via="bucket",
        source=str(parsed),
        target_path=dest_prefix,
        bytes_count=total,
        status_code=200,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"file_count": len(copied)},
    )

    return SyncResponse(dest=dest_prefix, files=copied, bytes_copied=total)


@router.post("/v1/shared-resources:sync", response_model=SyncResponse)
def shared_resources_sync(
    req: SharedResourceSyncRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
) -> SyncResponse:
    parsed, agent_id = resolve_source(settings, req.source)
    validate_shared_dest_path(req.dest_path, agent_id)
    _require_registered(hub, agent_id)

    allowed, retry = limiter.try_consume(parsed.bucket)
    if not allowed:
        raise RateLimited(retry)

    src_bucket = f"{parsed.org}/{parsed.bucket}"
    src_prefix = parsed.path

    files = hub.list_bucket_dir(src_bucket, src_prefix)
    _check_sync_caps(settings, files)

    dest_prefix = f"shared_resources/{req.dest_path}"

    copied = _execute_sync(hub, src_bucket, src_prefix, dest_prefix)
    total = sum(f.bytes for f in copied)

    audit.write(
        agent_id=agent_id,
        route="/v1/shared-resources:sync",
        via="bucket",
        source=str(parsed),
        target_path=dest_prefix,
        bytes_count=total,
        status_code=200,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"file_count": len(copied)},
    )

    return SyncResponse(dest=dest_prefix, files=copied, bytes_copied=total)
