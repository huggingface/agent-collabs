from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import APIError, InvalidRequest, StorageUnavailable, TooLarge
from app.hub import DownloadFailed, ListingFailed, StorageFailed
from app.routes import (
    agents,
    channels,
    client,
    digest,
    health,
    inbox,
    jobs,
    leaderboard,
    me,
    messages,
    results,
    sync,
    traces,
    updates,
)


try:  # huggingface_hub 2.x ships its httpx fork as `httpx2`
    from httpx2 import HTTPError as HubHTTPError
except ImportError:
    from httpx import HTTPError as HubHTTPError


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

# The long-poll waiter registry (app/notify.py) lives in this process's memory,
# so a wake can only reach waiters parked on the same worker. Stated at startup
# because the failure mode is silent: with two workers roughly half of every
# `wait=` would stop being woken and just time out, looking exactly like a quiet
# board. The Dockerfile CMD pins `--workers 1` for this reason.
logging.getLogger(__name__).info(
    "long-poll notifier is in-process — this app MUST run with a single uvicorn "
    "worker (see the Dockerfile CMD); with more, wakes reach only the worker "
    "that served the write and every other wait= degrades to a full timeout"
)

app = FastAPI(title="bucket-sync", version="1.5.0")

app.include_router(health.router)
app.include_router(digest.router)
app.include_router(me.router)
app.include_router(agents.router)
app.include_router(messages.router)
app.include_router(results.router)
app.include_router(inbox.router)
app.include_router(updates.router)
app.include_router(leaderboard.router)
app.include_router(sync.router)
app.include_router(jobs.router)
app.include_router(channels.router)
app.include_router(traces.router)
app.include_router(client.router)


@app.exception_handler(APIError)
async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=headers)


@app.exception_handler(HubHTTPError)
@app.exception_handler(StorageFailed)
@app.exception_handler(ListingFailed)
@app.exception_handler(DownloadFailed)
async def _storage_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """An uncaught storage failure (HfHubHTTPError, the connection/timeout
    error under it, a Xet failure, or a cold-folder listing or batch download
    that failed) is a retryable 503, not a 500 — never a partial folder or a
    false 404. Logs the type and status only: exception text can carry signed
    URLs or Xet credentials."""
    status = getattr(exc, "status", None) or getattr(getattr(exc, "response", None), "status_code", None)
    log.warning(
        "storage backend failed: %s %s (type=%s status=%s)",
        request.method, request.url.path, getattr(exc, "type_name", type(exc).__name__), status,
    )
    return await _api_error_handler(request, StorageUnavailable(status))


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Request-shape errors in the standard error body, one line per problem."""
    errors = exc.errors()
    message = "\n".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
    )
    too_long = all(e["type"] == "string_too_long" for e in errors)
    err = TooLarge(message) if too_long else InvalidRequest(message)
    return await _api_error_handler(request, err)
