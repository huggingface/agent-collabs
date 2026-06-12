"""Taskforces (§18): named central-bucket subdirectories for group efforts.

A taskforce exists iff ``taskforces/{name}/README.md`` exists — creating one
IS writing its README, so the "every taskforce has a README" rule is a
structural invariant, not a policy. All endpoints share one read-model folder
(the recursive ``taskforces/`` tree listing): browsing any number of
taskforces costs at most one bucket listing per TTL window, and every write
is write-through into that same folder.
"""
from __future__ import annotations

import mimetypes
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, Response

from app.audit import AuditLogger
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
    NotFound,
    RateLimited,
    TaskforceExists,
    TaskforceNotFound,
)
from app.frontmatter import merge, parse, serialise
from app.hub import HubClient, ListedFile
from app.listing import STAMP_LEN, list_message_like
from app.models import (
    DigestTaskforces,
    MessageListing,
    MessageRecord,
    TaskforceCreateRequest,
    TaskforceCreateResponse,
    TaskforceDetail,
    TaskforceFileInfo,
    TaskforceFileListing,
    TaskforceFilePostRequest,
    TaskforceFileResponse,
    TaskforceListing,
    TaskforceSummary,
)
from app.naming import (
    agent_from_filename,
    stamp_yaml,
    taskforce_file_path,
    taskforce_note_path,
    taskforce_readme_path,
    utc_now,
)
from app.rate_limit import CompoundLimiter
from app.read_model import ReadModel, Record
from app.routes.messages import require_registered
from app.validation import (
    resolve_source,
    validate_agent_id,
    validate_path_components,
    validate_taskforce_dest_path,
    validate_taskforce_name,
)


router = APIRouter()

# The single read-model folder shared by every taskforce endpoint (§18.4).
FOLDER = "taskforces"

_STAMPED_RE = re.compile(r"^\d{8}-\d{6}-\d{3}_")


def _is_readme(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() == "readme.md"


def _grouped(read_model: ReadModel) -> dict[str, list[ListedFile]]:
    """All listed taskforce files grouped by taskforce name; groups without a
    README (e.g. mid-prune leftovers) are not taskforces and are dropped."""
    groups: dict[str, list[ListedFile]] = {}
    for e in read_model.listing(FOLDER):
        rel = e.rel_path.removeprefix("taskforces/")
        name, _, rest = rel.partition("/")
        if not rest:
            continue  # stray file directly under taskforces/
        groups.setdefault(name, []).append(e)
    return {
        n: fs
        for n, fs in groups.items()
        if any(f.rel_path == taskforce_readme_path(n) for f in fs)
    }


def _require_taskforce(read_model: ReadModel, name: str) -> list[ListedFile]:
    prefix = f"taskforces/{name}/"
    entries = [e for e in read_model.listing(FOLDER) if e.rel_path.startswith(prefix)]
    if not any(e.rel_path == taskforce_readme_path(name) for e in entries):
        raise TaskforceNotFound(name)
    return entries


def _note_records(
    read_model: ReadModel, entries: list[ListedFile]
) -> list[Record]:
    paths = [
        e.rel_path
        for e in entries
        if e.rel_path.endswith(".md") and not _is_readme(e.rel_path)
    ]
    recs = read_model.records_for(FOLDER, paths)
    return sorted(recs.values(), key=lambda r: r.filename)


def _contributors(
    entries: list[ListedFile], registered: set[str], creator: str | None
) -> list[str]:
    """Derived, not declared (§18.4): stamped note filenames parse to their
    author; named files match a registered agent's ``_{agent_id}`` marker."""
    found: set[str] = set()
    for e in entries:
        if _is_readme(e.rel_path):
            continue
        leaf = e.rel_path.rsplit("/", 1)[-1]
        if leaf.endswith(".md") and _STAMPED_RE.match(leaf):
            author = agent_from_filename(leaf)
            if author:
                found.add(author)
        else:
            for a in registered:
                if re.search(rf"_{re.escape(a)}(?![a-z0-9-])", e.rel_path):
                    found.add(a)
    if creator:
        found.discard(creator)
    out = [creator] if creator else []
    out.extend(sorted(found))
    return out


def _excerpt(body: str, limit: int = 160) -> str:
    """First prose line of the README (headings are usually just the name);
    falls back to the first heading when there is nothing else."""
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


def _last_activity(entries: list[ListedFile]) -> str | None:
    stamps = []
    for e in entries:
        leaf = e.rel_path.rsplit("/", 1)[-1]
        if leaf.endswith(".md") and _STAMPED_RE.match(leaf):
            stamps.append(leaf[:STAMP_LEN])
    return max(stamps) if stamps else None


def _created_compact(created: str | None) -> str:
    """The README's human-readable ``created`` stamp as a compact stamp, so it
    can order against note-filename stamps; unparseable → '' (sorts last)."""
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


def taskforce_digest(read_model: ReadModel) -> DigestTaskforces:
    """The digest's taskforce block (§16.5, §18.4): count + newest by creation."""
    groups = _grouped(read_model)
    readmes = read_model.records_for(
        FOLDER, [taskforce_readme_path(n) for n in groups]
    )

    def _key(n: str) -> str:
        rec = readmes.get(taskforce_readme_path(n))
        return _created_compact(_fm_str(rec.frontmatter, "created")) if rec else ""

    newest = sorted(groups, key=lambda n: (_key(n), n), reverse=True)[:5]
    return DigestTaskforces(count=len(groups), newest=newest)


# ───────────────────────── writes ─────────────────────────


@router.post("/v1/taskforces", response_model=TaskforceCreateResponse, status_code=201)
def create_taskforce(
    req: TaskforceCreateRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    dedup: PromotionLRU = Depends(get_dedup),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    raw_limiter: CompoundLimiter = Depends(get_raw_message_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> TaskforceCreateResponse:
    now = utc_now()
    validate_taskforce_name(req.name)
    target = taskforce_readme_path(req.name)

    raw_bytes: bytes | None = None
    if req.source is not None:
        parsed, agent_id = resolve_source(settings, req.source)
        require_registered(read_model, hub, agent_id)
        allowed, retry = bucket_limiter.try_consume(parsed.bucket)
        if not allowed:
            raise RateLimited(retry)
        raw_bytes = hub.read_bytes(parsed)
        client_fm, body = parse(raw_bytes.decode("utf-8"))
        via = "bucket"
    else:
        assert req.agent_id is not None and req.body is not None
        agent_id = req.agent_id
        validate_agent_id(agent_id)
        require_registered(read_model, hub, agent_id)
        allowed, retry = raw_limiter.try_consume(agent_id)
        if not allowed:
            raise RateLimited(retry)
        client_fm, body = {}, req.body
        via = "raw"

    existing = read_model.record(FOLDER, f"{req.name}/README.md")
    if existing is not None:
        creator = _fm_str(existing.frontmatter, "creator")
        if creator != agent_id:
            raise TaskforceExists(req.name, creator)
        created = False
        server_fm = {
            "taskforce": req.name,
            "creator": creator,
            "created": existing.frontmatter.get("created"),
            "updated": stamp_yaml(now),
            "via": via,
        }
    else:
        created = True
        server_fm = {
            "taskforce": req.name,
            "creator": agent_id,
            "created": stamp_yaml(now),
            "via": via,
        }

    if raw_bytes is not None:
        dup = dedup.get(content_hash(raw_bytes), f"taskforces/{req.name}")
        if dup:
            raise AlreadyPromoted(dup)

    merged = merge(client_fm, server_fm)
    content = serialise(merged, body)
    nbytes = len(content.encode("utf-8"))
    hub.write_text_central(target, content)
    read_model.write_through(target, merged, body, nbytes, folder=FOLDER)
    if raw_bytes is not None:
        dedup.record(content_hash(raw_bytes), f"taskforces/{req.name}", "README.md")

    audit.write(
        agent_id=agent_id,
        route="/v1/taskforces",
        via=via,
        source=req.source,
        target_path=target,
        bytes_count=nbytes,
        status_code=201 if created else 200,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"taskforce": req.name, "created": created},
    )

    if not created:
        response.status_code = 200
    return TaskforceCreateResponse(name=req.name, via=via, path=target, created=created)


@router.post(
    "/v1/taskforces/{name}/files",
    response_model=TaskforceFileResponse,
    status_code=201,
)
def post_taskforce_file(
    name: str,
    req: TaskforceFilePostRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    audit: AuditLogger = Depends(get_audit),
    dedup: PromotionLRU = Depends(get_dedup),
    bucket_limiter: CompoundLimiter = Depends(get_bucket_write_limiter),
    raw_limiter: CompoundLimiter = Depends(get_raw_message_limiter),
    read_model: ReadModel = Depends(get_read_model),
) -> TaskforceFileResponse:
    now = utc_now()
    validate_taskforce_name(name)
    _require_taskforce(read_model, name)
    dest_folder = f"taskforces/{name}"
    route = "/v1/taskforces/{name}/files"

    data: bytes | None = None
    if req.source is not None:
        parsed, agent_id = resolve_source(settings, req.source)
        require_registered(read_model, hub, agent_id)
        allowed, retry = bucket_limiter.try_consume(parsed.bucket)
        if not allowed:
            raise RateLimited(retry)
        data = hub.read_bytes(parsed)

        if req.dest_path is not None:
            # Named file: byte-identical copy, attribution by marker (§18.3).
            # No dedup — re-promoting your own path is the update mechanism.
            validate_taskforce_dest_path(req.dest_path, agent_id)
            target = taskforce_file_path(name, req.dest_path)
            hub.write_bytes_central(target, data)
            if target.endswith(".md"):
                try:
                    fm, body = parse(data.decode("utf-8"))
                except Exception:
                    fm, body = {}, data.decode("utf-8", errors="replace")
                read_model.write_through(target, fm, body, len(data), folder=FOLDER)
            else:
                # Listing freshness only; binaries are never content-cached.
                read_model.write_through(target, {}, "", len(data), folder=FOLDER)
            audit.write(
                agent_id=agent_id,
                route=route,
                via="bucket",
                source=req.source,
                target_path=target,
                bytes_count=len(data),
                status_code=201,
                caller_ip=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                extra={"taskforce": name, "kind": "file"},
            )
            return TaskforceFileResponse(
                kind="file", filename=req.dest_path, via="bucket", path=target
            )

        # Bucket note: message-shaped promotion into the taskforce.
        client_fm, body = parse(data.decode("utf-8"))
        dup = dedup.get(content_hash(data), dest_folder)
        if dup:
            raise AlreadyPromoted(dup)
        client_fm.setdefault("type", "note")
        via = "bucket"
    else:
        assert req.agent_id is not None and req.body is not None
        agent_id = req.agent_id
        validate_agent_id(agent_id)
        require_registered(read_model, hub, agent_id)
        allowed, retry = raw_limiter.try_consume(agent_id)
        if not allowed:
            raise RateLimited(retry)
        client_fm, body = {"type": req.type or "note"}, req.body
        via = "raw"

    server_fm = {
        "agent": agent_id,
        "timestamp": stamp_yaml(now),
        "via": via,
        "taskforce": name,
    }
    merged = merge(client_fm, server_fm)
    target = taskforce_note_path(name, agent_id, now)
    filename = target.rsplit("/", 1)[-1]
    content = serialise(merged, body)
    nbytes = len(content.encode("utf-8"))
    hub.write_text_central(target, content)
    read_model.write_through(target, merged, body, nbytes, folder=FOLDER)
    if data is not None:
        dedup.record(content_hash(data), dest_folder, filename)

    audit.write(
        agent_id=agent_id,
        route=route,
        via=via,
        source=req.source,
        target_path=target,
        bytes_count=nbytes,
        status_code=201,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"taskforce": name, "kind": "note"},
    )
    return TaskforceFileResponse(kind="note", filename=filename, via=via, path=target)


# ───────────────────────── discovery ─────────────────────────


@router.get("/v1/taskforces", response_model=TaskforceListing)
def list_taskforces(
    q: str | None = None,
    limit: int | None = None,
    read_model: ReadModel = Depends(get_read_model),
) -> TaskforceListing:
    groups = _grouped(read_model)
    readmes = read_model.records_for(
        FOLDER, [taskforce_readme_path(n) for n in groups]
    )
    registered = read_model.registered_agents()

    keyed: list[tuple[str, TaskforceSummary]] = []
    for nm, entries in groups.items():
        readme = readmes.get(taskforce_readme_path(nm))
        fm = readme.frontmatter if readme else {}
        body = readme.body if readme else ""
        if q is not None:
            ql = q.lower()
            if ql not in nm.lower() and ql not in body.lower():
                continue
        creator = _fm_str(fm, "creator")
        created = _fm_str(fm, "created")
        notes = [
            e
            for e in entries
            if e.rel_path.endswith(".md") and not _is_readme(e.rel_path)
        ]
        last = _last_activity(entries)
        keyed.append(
            (
                max(last or "", _created_compact(created)),
                TaskforceSummary(
                    name=nm,
                    creator=creator,
                    created=created,
                    readme_excerpt=_excerpt(body),
                    contributors=_contributors(entries, registered, creator),
                    file_count=len(entries),
                    note_count=len(notes),
                    last_activity=last,
                ),
            )
        )
    # Most recently active first (discoverability is the point); name breaks ties.
    keyed.sort(key=lambda t: t[1].name)
    keyed.sort(key=lambda t: t[0], reverse=True)
    items = [s for _, s in keyed]
    matched = len(items)
    if limit is not None and 0 < limit < len(items):
        items = items[:limit]
    return TaskforceListing(count=len(groups), matched=matched, items=items)


@router.get("/v1/taskforces/{name}", response_model=TaskforceDetail)
def get_taskforce(
    name: str,
    read_model: ReadModel = Depends(get_read_model),
) -> TaskforceDetail:
    validate_taskforce_name(name)
    entries = _require_taskforce(read_model, name)
    readme_path = taskforce_readme_path(name)
    readme = read_model.records_for(FOLDER, [readme_path]).get(readme_path)
    if readme is None:
        raise NotFound(readme_path)  # transient content-fetch failure; retry
    fm = readme.frontmatter
    creator = _fm_str(fm, "creator")
    notes = _note_records(read_model, entries)
    recent = list(reversed(notes))[:5]
    return TaskforceDetail(
        name=name,
        creator=creator,
        created=_fm_str(fm, "created"),
        updated=_fm_str(fm, "updated"),
        readme=MessageRecord(filename="README.md", frontmatter=fm, body=readme.body),
        contributors=_contributors(entries, read_model.registered_agents(), creator),
        file_count=len(entries),
        note_count=len(notes),
        recent_notes=[
            MessageRecord(filename=r.filename, frontmatter=r.frontmatter, body=r.body)
            for r in recent
        ],
    )


@router.get("/v1/taskforces/{name}/notes", response_model=MessageListing)
def list_taskforce_notes(
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
    validate_taskforce_name(name)
    entries = _require_taskforce(read_model, name)
    return list_message_like(
        _note_records(read_model, entries),
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


@router.get("/v1/taskforces/{name}/files", response_model=TaskforceFileListing)
def list_taskforce_files(
    name: str,
    read_model: ReadModel = Depends(get_read_model),
) -> TaskforceFileListing:
    validate_taskforce_name(name)
    entries = _require_taskforce(read_model, name)
    prefix = f"taskforces/{name}/"
    items = [
        TaskforceFileInfo(path=e.rel_path.removeprefix(prefix), size=e.size)
        for e in sorted(entries, key=lambda e: e.rel_path)
    ]
    return TaskforceFileListing(count=len(items), items=items)


@router.get("/v1/taskforces/{name}/files/{file_path:path}")
def get_taskforce_file(
    name: str,
    file_path: str,
    hub: HubClient = Depends(get_hub),
    read_model: ReadModel = Depends(get_read_model),
) -> Response:
    validate_taskforce_name(name)
    entries = _require_taskforce(read_model, name)
    validate_path_components(file_path)
    target = taskforce_file_path(name, file_path)
    if not any(e.rel_path == target for e in entries):
        raise NotFound(target)
    data = hub.read_central_bytes_optional(target)
    if data is None:
        raise NotFound(target)
    if target.endswith(".md"):
        media = "text/markdown"
    else:
        media = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return Response(content=data, media_type=media)
