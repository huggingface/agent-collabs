from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import APIError
from app.routes import (
    agents,
    digest,
    health,
    inbox,
    jobs,
    leaderboard,
    messages,
    results,
    sync,
    taskforces,
    traces,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="bucket-sync", version="1.3.0")

app.include_router(health.router)
app.include_router(digest.router)
app.include_router(agents.router)
app.include_router(messages.router)
app.include_router(results.router)
app.include_router(inbox.router)
app.include_router(leaderboard.router)
app.include_router(sync.router)
app.include_router(jobs.router)
app.include_router(taskforces.router)
app.include_router(traces.router)


@app.exception_handler(APIError)
async def _api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    headers = getattr(exc, "headers", None)
    return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=headers)
