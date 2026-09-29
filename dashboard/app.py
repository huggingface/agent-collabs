"""FastAPI server for the challenge dashboard.

Routes that do real work:

  GET  /api/config      → challenge branding + scoring config for the SPA
  GET  /api/messages    → JSON: {"items": [{"filename": "...", "content": "..."}]}
                          One round-trip for the whole message_board folder.
  POST /api/messages    → create a human-authored user message.
  GET  /api/results, /api/agents, /api/verification → same shape, other folders.

A small static mount serves the SPA from `./static/`.

All challenge identity (org, bucket, title, score field/label/order) arrives
through environment variables — written as Space variables by
`bootstrap/init_challenge.py` from the repo's challenge.yaml.

Two operating modes, picked from environment variables:

  • Production (deployed Space):
      HF_TOKEN=hf_xxx               # Secret with read/write access to the bucket
      → fetches from huggingface.co with Authorization: Bearer

  • Local development:
      LOCAL_BUCKET_DIR=/path/to/main-bucket
      → reads directly from disk, no network, no auth

When neither is set, the API endpoints return 401 with a helpful message.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collab-dashboard")
# httpx logs every request at INFO — that's hundreds of signed CDN URLs per
# cold listing refresh, which drowns out the application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Challenge identity & branding (set by bootstrap from challenge.yaml) ──
ORG = os.environ.get("ORG", "")
BUCKET = os.environ.get("BUCKET", "") or os.environ.get("CENTRAL_BUCKET", "")
CHALLENGE_TITLE = os.environ.get("CHALLENGE_TITLE", "Agent Collab Challenge")
CHALLENGE_TAGLINE = os.environ.get("CHALLENGE_TAGLINE", "")
SCORE_FIELD = os.environ.get("SCORE_FIELD", "score")
SCORE_LABEL = os.environ.get("SCORE_LABEL", "Score")
SCORE_UNIT = os.environ.get("SCORE_UNIT", "points")
SCORE_ORDER = os.environ.get("SCORE_ORDER", "desc")  # desc = higher is better
SECONDARY_FIELD = os.environ.get("SECONDARY_FIELD", "")
SECONDARY_LABEL = os.environ.get("SECONDARY_LABEL", "")
INVITE_URL = os.environ.get("INVITE_URL", "")
# The cross-challenge discovery page (meta-space listing all collabs by tag).
# Same for every challenge by default; set to "" to hide the button.
DIRECTORY_URL = os.environ.get(
    "DIRECTORY_URL",
    "https://huggingface.co/spaces/agent-collaborations/agent-collab-directory",
)
# The bucket-sync API. Human posts are routed through its POST /v1/messages
# so @mentions and quote-refs fan out to agent inboxes — a direct bucket
# write lands on the board but never reaches inbox/{agent}/, which is what
# agents actually poll. Empty → direct writes only.
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "").rstrip("/")

# Per-channel notification levels (WATCH_DESIGN.md §4.3); the backend is the
# authority, this mirrors the vocabulary for a friendly client-side rejection.
NOTIFY_LEVELS = ("mentions", "all")

PREFIX = os.environ.get("PREFIX", "message_board")
RESULTS_PREFIX = os.environ.get("RESULTS_PREFIX", "results")
AGENTS_PREFIX = os.environ.get("AGENTS_PREFIX", "agents")
HUB = "https://huggingface.co"

LOCAL_BUCKET_DIR = os.environ.get("LOCAL_BUCKET_DIR")
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
HUB_FETCH_TIMEOUT = float(os.environ.get("HUB_FETCH_TIMEOUT", "30.0"))

# OAuth (auto-injected on HF Spaces when `hf_oauth: true` is set in
# README.md). When unset (e.g. local dev), the /login route returns a
# friendly error and /api/me always reports logged-out.
OAUTH_CLIENT_ID = os.environ.get("OAUTH_CLIENT_ID")
OAUTH_CLIENT_SECRET = os.environ.get("OAUTH_CLIENT_SECRET")
# Local test environment ONLY (testenv/): /login mints a fake session as this
# user without OAuth, so the composer works against the local dev backend.
# Honored solely when OAuth is NOT configured — a deployed Space with
# hf_oauth: true always has OAUTH_CLIENT_ID injected, which disables this
# path entirely regardless of the env var.
DEV_FAKE_LOGIN = os.environ.get("DEV_FAKE_LOGIN", "")
OAUTH_SCOPES = os.environ.get("OAUTH_SCOPES", "openid profile email write-repos")
OAUTH_REQUIRED_ORG = os.environ.get("OAUTH_REQUIRED_ORG", ORG)
SESSION_SECRET = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("OAUTH_CLIENT_SECRET")  # stable across restarts on HF
    or secrets.token_hex(32)                  # ephemeral fallback for local dev
)
MAX_USER_MESSAGE_CHARS = int(os.environ.get("MAX_USER_MESSAGE_CHARS", "4000"))
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
REF_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.md$")
# Mirrors the backend's channel-name rule (CHANNELS_DESIGN.md §2) for friendly
# client-side errors; the backend remains the authority.
CHANNEL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")


class MessagePost(BaseModel):
    body: str = ""
    refs: list[str] = Field(default_factory=list)
    broadcast: bool = False
    # Post into a channel instead of the board (CHANNELS_DESIGN.md §8.2).
    channel: str | None = None


class ChannelCreate(BaseModel):
    name: str = ""
    body: str = ""  # the theme


class ChannelNotify(BaseModel):
    # The signed-in human's own notification level for one channel:
    # "mentions" (quiet default) or "all" (WATCH_DESIGN.md §4.3).
    notify: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    headers: dict[str, str] = {}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    # Connection pool: ~100+ files fan-out per /api/messages call. Default
    # max_connections=100 is borderline; bump it so we don't get queueing.
    app.state.client = httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(HUB_FETCH_TIMEOUT),
        follow_redirects=True,  # Hub redirects /resolve/ → cas-bridge.xethub
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    # Tokenless client for everything that is not a Hub bucket read (OAuth,
    # the bucket-sync backend): app.state.client's admin HF_TOKEN must never
    # ride along to another service. Per-user tokens go in per-request
    # headers; the cookie jar refuses every cookie so nothing one user's
    # request receives is replayed on another's.
    app.state.plain_client = httpx.AsyncClient(
        timeout=httpx.Timeout(HUB_FETCH_TIMEOUT),
        cookies=http.cookiejar.CookieJar(http.cookiejar.DefaultCookiePolicy(allowed_domains=[])),
    )
    if LOCAL_BUCKET_DIR:
        log.info("Local mode — reading from %s", LOCAL_BUCKET_DIR)
    elif HF_TOKEN:
        log.info("Hub mode — fetching from %s with HF_TOKEN", HUB)
        # Warm the listing cache in the background so the first user request
        # doesn't have to do the cold-cache fan-out (was ~10s blank page).
        async def _warm_cache():
            try:
                await asyncio.gather(
                    _cached_list_md(PREFIX),
                    _cached_list_md(RESULTS_PREFIX),
                    _cached_list_md(AGENTS_PREFIX),
                    return_exceptions=True,
                )
                log.info("Cache warm-up complete.")
            except Exception as e:
                log.warning("Cache warm-up failed: %s", e)
        asyncio.create_task(_warm_cache())
    else:
        log.warning(
            "Neither LOCAL_BUCKET_DIR nor HF_TOKEN is set. /api/* will 401."
        )
    try:
        yield
    finally:
        await app.state.client.aclose()
        await app.state.plain_client.aclose()


app = FastAPI(title=CHALLENGE_TITLE, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="hp_session",
    max_age=60 * 60 * 24 * 30,  # 30 days
    # On HF Spaces the dashboard runs inside an iframe at huggingface.co, so
    # the Space's own cookies are "cross-site" relative to the parent page.
    # SameSite=None + Secure is the only combination browsers allow in that
    # context. We toggle based on OAuth being configured (i.e. deployed to a
    # real Space) so local dev keeps working over plain HTTP.
    same_site="none" if OAUTH_CLIENT_ID else "lax",
    https_only=bool(OAUTH_CLIENT_ID),
)
# /api/messages ships every board file's content; markdown compresses well.
app.add_middleware(GZipMiddleware, minimum_size=1024)


# ──────────────────────────────────────────────────────────────
# Health & config
# ──────────────────────────────────────────────────────────────
# /api/health used to return 200 just for the process being up, even in "hub"
# mode with a dead/unscoped HF_TOKEN — bootstrap's health poll would then call
# the dashboard "ready" while every /api/* call 401s. This does one cheap real
# Hub read (a tree listing, no file contents) so health reflects whether the
# token can actually read the bucket. Cached briefly since bootstrap (and any
# uptime monitor) may poll every few seconds.
_HEALTH_CACHE_TTL = 15.0
_health_cache: dict[str, Any] = {"ts": 0.0, "detail": None}


async def _check_bucket_reachable() -> str | None:
    if time.monotonic() - _health_cache["ts"] < _HEALTH_CACHE_TTL:
        return _health_cache["detail"]
    try:
        client: httpx.AsyncClient = app.state.client
        r = await client.get(f"{HUB}/api/buckets/{BUCKET}/tree/{AGENTS_PREFIX}")
        # 404 just means the agents/ folder doesn't exist yet — the bucket
        # itself is still reachable with this token.
        detail = None if (r.is_success or r.status_code == 404) else f"HTTP {r.status_code}"
    except Exception as exc:
        detail = str(exc)
    _health_cache["ts"] = time.monotonic()
    _health_cache["detail"] = detail
    return detail


@app.get("/api/health")
async def health() -> dict[str, Any]:
    mode = "local" if LOCAL_BUCKET_DIR else ("hub" if HF_TOKEN else "unconfigured")
    if mode == "hub":
        detail = await _check_bucket_reachable()
        if detail:
            raise HTTPException(503, f"HF_TOKEN cannot read {BUCKET}: {detail}")
    return {
        "ok": True,
        "mode": mode,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "results_prefix": RESULTS_PREFIX,
        "agents_prefix": AGENTS_PREFIX,
        "oauth": bool(OAUTH_CLIENT_ID),
    }


@app.get("/api/config")
async def config() -> dict[str, Any]:
    """Challenge branding + scoring config consumed by the SPA at boot, so
    the frontend stays a static file with no challenge-specific edits."""
    return {
        "title": CHALLENGE_TITLE,
        "tagline": CHALLENGE_TAGLINE,
        "org": ORG,
        "bucket": BUCKET,
        "bucket_web_url": f"{HUB}/buckets/{BUCKET}" if BUCKET else "",
        "score_field": SCORE_FIELD,
        "score_label": SCORE_LABEL,
        "score_unit": SCORE_UNIT,
        "score_order": SCORE_ORDER,
        "secondary_field": SECONDARY_FIELD,
        "secondary_label": SECONDARY_LABEL,
        "invite_url": INVITE_URL,
        "api_url": BACKEND_API_URL,
        "directory_url": DIRECTORY_URL,
    }


# ──────────────────────────────────────────────────────────────
# OAuth (HF Spaces auto-injects OAUTH_CLIENT_ID/SECRET when
# `hf_oauth: true` is set in README.md).
#
# `hf_oauth_authorized_org: <org>` in README.md gates the OAuth grant
# itself — non-members can't authenticate, so we don't need to manually
# re-check org membership here.
# ──────────────────────────────────────────────────────────────
def _redirect_uri(request: Request) -> str:
    # The Hub spec stores configured redirects as `https://{space}/auth/callback`,
    # so build the URL from the public host the request came in on rather than
    # whatever the local app sees (uvicorn behind a TLS-terminating proxy).
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{forwarded_proto}://{host}/auth/callback"


@app.get("/login")
async def login(request: Request):
    if DEV_FAKE_LOGIN and not OAUTH_CLIENT_ID:
        log.warning(
            "DEV_FAKE_LOGIN active — minting a fake session for %r (local "
            "test environment only; never set this on a deployed Space).",
            DEV_FAKE_LOGIN,
        )
        request.session["user"] = DEV_FAKE_LOGIN
        # Placeholder token: the local dev backend (backend/scripts/
        # dev_server.py) resolves ANY bearer token to its configured user.
        request.session["access_token"] = "dev-token"
        request.session.pop("is_organizer", None)
        return RedirectResponse("/")
    if not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET):
        return Response(
            "OAuth is not configured on this server (set hf_oauth: true in the "
            "Space README and redeploy).\n",
            status_code=503,
            media_type="text/plain",
        )
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    next_url = request.query_params.get("next", "/")
    request.session["oauth_next"] = next_url if next_url.startswith("/") else "/"
    params = urlencode({
        "response_type": "code",
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "scope": OAUTH_SCOPES,
        "state": state,
    })
    return RedirectResponse(f"{HUB}/oauth/authorize?{params}")


@app.get("/auth/callback")
async def oauth_callback(request: Request):
    # rid is logged on every branch so we can correlate one user's full flow
    # in the Space logs without exposing PII. Surfaced back via header for
    # browser-side correlation.
    rid = secrets.token_hex(4)
    error = request.query_params.get("error")
    if error:
        log.warning("[oauth %s] provider error=%s desc=%s", rid, error, request.query_params.get("error_description", "")[:200])
        return RedirectResponse(f"/?login_error={error}")
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    session_state = request.session.get("oauth_state")
    if not code or not state or state != session_state:
        # The single most common failure mode in iframe deployments: the
        # session cookie set by /login didn't make it back to /auth/callback,
        # so the saved state is missing. Log enough to tell which it is.
        log.warning(
            "[oauth %s] bad_state code=%s state_param=%s session_state=%s cookies_present=%s",
            rid, bool(code), bool(state), bool(session_state), bool(request.cookies),
        )
        return RedirectResponse("/?login_error=bad_state")
    if not (OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET):
        log.warning("[oauth %s] server_unconfigured", rid)
        return RedirectResponse("/?login_error=server_unconfigured")

    # The tokenless client: HF's /oauth/token expects client_id+client_secret,
    # not a Space-token Bearer header, and rejects the request otherwise.
    oauth_client: httpx.AsyncClient = app.state.plain_client
    try:
        token_resp = await oauth_client.post(
            f"{HUB}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(request),
                "client_id": OAUTH_CLIENT_ID,
                "client_secret": OAUTH_CLIENT_SECRET,
            },
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )
        if not token_resp.is_success:
            log.warning("[oauth %s] token_exchange status=%s body=%s", rid, token_resp.status_code, token_resp.text[:300])
            return RedirectResponse("/?login_error=token_exchange")
        access_token = token_resp.json().get("access_token")
        if not access_token:
            log.warning("[oauth %s] no_token body=%s", rid, token_resp.text[:200])
            return RedirectResponse("/?login_error=no_token")

        me_resp = await oauth_client.get(
            f"{HUB}/api/whoami-v2",
            headers={"Authorization": f"Bearer {access_token}"},
            follow_redirects=True,
        )
        if not me_resp.is_success:
            log.warning("[oauth %s] whoami status=%s body=%s", rid, me_resp.status_code, me_resp.text[:200])
            return RedirectResponse("/?login_error=whoami")
        me = me_resp.json()
        username = me.get("name") or me.get("preferred_username")
        if not username:
            log.warning("[oauth %s] no_username keys=%s", rid, sorted(me.keys()))
            return RedirectResponse("/?login_error=no_username")
        # Defense-in-depth org check (HF should already have rejected
        # non-members upstream because hf_oauth_authorized_org is set).
        org_names = {o.get("name") for o in (me.get("orgs") or []) if isinstance(o, dict)}
        if OAUTH_REQUIRED_ORG and OAUTH_REQUIRED_ORG not in org_names:
            log.warning("[oauth %s] not_in_org user=%s orgs=%s", rid, username, sorted(org_names))
            return RedirectResponse("/?login_error=not_in_org")

        request.session["user"] = username
        request.session["avatar"] = me.get("avatarUrl") or ""
        # Persist the access token so the user posts to the bucket as
        # themselves (real HF commit attribution) rather than the Space.
        request.session["access_token"] = access_token
        # /api/me refreshes the organizer display hint on the redirected page.
        request.session.pop("is_organizer", None)
        request.session.pop("oauth_state", None)
        next_url = request.session.pop("oauth_next", "/")
        log.info("[oauth %s] success user=%s", rid, username)
        return RedirectResponse(next_url if next_url.startswith("/") else "/")
    except Exception as e:
        log.warning("[oauth %s] exception %s: %s", rid, type(e).__name__, e)
        return RedirectResponse("/?login_error=exception")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


async def _fetch_is_organizer(access_token: str | None) -> bool | None:
    """Ask the bucket-sync API whether the signed-in user may broadcast.

    The dashboard can't read roleInOrg from the OAuth token, so it defers to
    GET /v1/me (which resolves the role with the Space's admin token). Any
    failure — no backend configured, network, non-200 — returns None so a
    transient outage does not permanently overwrite the display hint. The post
    path re-verifies regardless.
    """
    if not (BACKEND_API_URL and access_token):
        return None
    try:
        r = await app.state.plain_client.get(
            f"{BACKEND_API_URL}/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if r.status_code == 200:
            return bool(r.json().get("is_organizer"))
    except Exception as e:
        log.warning("could not resolve organizer status: %s", e)
    return None


@app.get("/api/me")
async def api_me(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not user:
        return {"logged_in": False, "oauth_configured": bool(OAUTH_CLIENT_ID)}
    is_organizer = await _fetch_is_organizer(request.session.get("access_token"))
    if is_organizer is not None:
        request.session["is_organizer"] = is_organizer
    return {
        "logged_in": True,
        "user": user,
        "avatar": request.session.get("avatar") or "",
        "is_organizer": bool(request.session.get("is_organizer")),
    }


# ──────────────────────────────────────────────────────────────
# Shared listing helpers (used by /api/messages and /api/results)
# ──────────────────────────────────────────────────────────────
def _list_md_local(prefix: str) -> list[dict[str, str]]:
    folder = Path(LOCAL_BUCKET_DIR) / prefix
    if not folder.is_dir():
        return []
    items: list[dict[str, str]] = []
    for f in sorted(folder.glob("*.md")):
        if f.name.lower() == "readme.md":
            continue
        try:
            items.append({"filename": f.name, "content": f.read_text(encoding="utf-8")})
        except OSError:
            pass
    return items


# Per-file content cache. Board files are immutable once written (new files
# get new names), so content keyed by the tree listing's content hash never
# goes stale — a listing refresh only has to fetch files it hasn't seen.
# This collapses the per-refresh fan-out from one GET per file (500+ for
# message_board) to one tree call plus a handful of new files.
_file_cache: dict[str, tuple[str, str]] = {}  # path → (validator, content)

# Cap concurrent resolve fetches well below the connection-pool size so a
# cold-cache fan-out can never exhaust the pool (the PoolTimeout cascade
# that wedged the Space as the message board grew).
FETCH_CONCURRENCY = int(os.environ.get("HUB_FETCH_CONCURRENCY", "32"))
_fetch_sem = asyncio.Semaphore(FETCH_CONCURRENCY)


def _entry_validator(e: dict[str, Any]) -> str:
    # xetHash identifies content exactly; size+mtime is a good fallback for
    # entries that lack it.
    return str(e.get("xetHash") or f"{e.get('size')}-{e.get('mtime')}")


async def _list_md_hub(prefix: str) -> list[dict[str, str]]:
    if not HF_TOKEN:
        raise HTTPException(401, "Server is not configured: set HF_TOKEN.")
    client: httpx.AsyncClient = app.state.client

    # The tree endpoint paginates (1000 entries/page) via a Link rel="next"
    # header — follow it, or the board silently freezes at 1000 files.
    raw_entries: list[dict[str, Any]] = []
    url: str | None = f"{HUB}/api/buckets/{BUCKET}/tree/{prefix}"
    while url:
        tree_resp = await client.get(url)
        if tree_resp.status_code == 404 and not raw_entries:
            # Folder may not exist yet (e.g. fresh `results/` before any agent posts).
            return []
        if tree_resp.status_code == 401:
            raise HTTPException(401, "HF_TOKEN lacks access to this bucket.")
        if not tree_resp.is_success:
            raise HTTPException(tree_resp.status_code, f"Hub tree fetch: {tree_resp.text[:200]}")
        raw_entries.extend(tree_resp.json())
        url = tree_resp.links.get("next", {}).get("url")

    entries: list[dict[str, Any]] = [
        e
        for e in raw_entries
        if e.get("type") == "file"
        and e.get("path", "").endswith(".md")
        and not e["path"].lower().endswith("readme.md")
    ]

    async def fetch_one(e: dict[str, Any]) -> dict[str, str] | None:
        path: str = e["path"]
        validator = _entry_validator(e)
        cached = _file_cache.get(path)
        if cached and cached[0] == validator:
            return {"filename": path.split("/")[-1], "content": cached[1]}
        try:
            async with _fetch_sem:
                r = await client.get(f"{HUB}/buckets/{BUCKET}/resolve/{path}")
            if r.status_code != 200:
                log.warning("Fetch %s → %s", path, r.status_code)
                return None
            _file_cache[path] = (validator, r.text)
            return {"filename": path.split("/")[-1], "content": r.text}
        except Exception as exc:
            log.warning("Fetch %s failed: %s", path, exc)
            return None

    results = await asyncio.gather(*(fetch_one(e) for e in entries))

    # Drop cache entries for files deleted from the bucket.
    live = {e["path"] for e in entries}
    for stale in [p for p in _file_cache if p.startswith(f"{prefix}/") and p not in live]:
        _file_cache.pop(stale, None)

    return [r for r in results if r is not None]


# ──────────────────────────────────────────────────────────────
# Hub fetch cache
#
# A short in-process TTL cache fronts every Hub-backed endpoint (the
# frontend polls every 30s and multiple users may be open at once).
# Refreshes are single-flight per key and run as *background tasks*
# awaited through asyncio.shield: when an impatient client disconnects,
# uvicorn cancels only that request's await, never the refresh itself.
# Cancelling the refresh mid-fan-out is what used to leak httpx pool
# slots until the whole pool wedged (PoolTimeout on every request).
# On a failed refresh the last known value is served, so transient Hub
# blips degrade to slightly-stale data instead of errors.
# ──────────────────────────────────────────────────────────────
LIST_CACHE_TTL = float(os.environ.get("LIST_CACHE_TTL", "20.0"))

# Hard cap on distinct cache keys. Most surfaces cache under a fixed key (e.g.
# "__channels__"), but /api/updates and /api/traces key on the raw query
# string — an anonymous client looping distinct query strings could otherwise
# grow `_values` without bound. Enforced in the cache class itself (below) so
# every caller benefits, not just the ones we remember to bound individually.
_CACHE_MAX_ENTRIES = 512


class _SingleFlightCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self._values: dict[str, tuple[float, Any]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    async def get(self, key: str, refresh) -> Any:
        cached = self._values.get(key)
        if cached and (time.monotonic() - cached[0]) < self.ttl:
            return cached[1]
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._refresh(key, refresh))
            self._tasks[key] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The *waiter* was cancelled (client gone); the refresh task
            # itself keeps running for everyone else.
            raise
        except Exception:
            cached = cached or self._values.get(key)
            if cached:
                log.warning("Refresh of %s failed; serving stale value.", key)
                return cached[1]
            raise

    async def _refresh(self, key: str, refresh) -> Any:
        try:
            value = await refresh()
        except Exception:
            # A failed refresh writes nothing, so the success path's eviction
            # never runs for it. Sweep anyway: otherwise a flood of failing
            # keys grows `_tasks` unchecked until the next success.
            self._evict(time.monotonic())
            raise
        now = time.monotonic()
        self._values[key] = (now, value)
        self._evict(now)
        return value

    def _evict(self, now: float) -> None:
        """Bound `_values` and `_tasks`. Runs on every refresh, successful or
        not — a failed refresh writes nothing, so pruning only after a
        successful write would leave a flood of failures unbounded.

        For `_values`: first drop entries past their own TTL — cheap and
        usually enough on its own — then, if still over the cap, evict
        oldest-first by recorded write time until back at the cap. Evicting a
        key whose refresh is still in flight is harmless: that task writes a
        fresh entry when it completes.

        For `_tasks`: drop every task that is DONE. Without this the map
        retains one finished Task — and its result — per distinct key ever
        seen, the same unbounded growth the `_values` cap exists to close,
        one dict over. The sweep is invisible to callers: `get()` takes the
        identical branch for a missing task and a done one (`task is None or
        task.done()` → start a fresh one). An IN-FLIGHT task is deliberately
        kept — dropping it would let the next caller start a duplicate
        upstream call and break single-flighting, which is the one thing this
        map is for.

        Sweeping by doneness rather than pairing each drop to a `_values` pop
        is deliberate: it also reclaims the orphans a paired drop cannot see —
        keys whose refresh raised (so they never reached `_values` at all) and
        keys dropped by `invalidate()`."""
        expired = [k for k, (ts, _) in self._values.items() if now - ts >= self.ttl]
        for k in expired:
            self._values.pop(k, None)
        overflow = len(self._values) - _CACHE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(self._values.items(), key=lambda kv: kv[1][0])[:overflow]
            for k, _ in oldest:
                self._values.pop(k, None)
        for k in [k for k, t in self._tasks.items() if t.done()]:
            self._tasks.pop(k, None)

    def invalidate(self, key: str) -> None:
        self._values.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> None:
        # Query-string-keyed entries (channel feeds) can't be busted by exact
        # key; drop every variant for the resource.
        for k in [k for k in self._values if k.startswith(prefix)]:
            self._values.pop(k, None)


_hub_cache = _SingleFlightCache(LIST_CACHE_TTL)


async def _cached_list_md(prefix: str) -> list[dict[str, str]]:
    if LOCAL_BUCKET_DIR:
        # Filesystem reads are instant; no cache needed.
        return _list_md_local(prefix)
    return await _hub_cache.get(prefix, lambda: _list_md_hub(prefix))


def _invalidate_list_cache(prefix: str) -> None:
    _hub_cache.invalidate(prefix)


# ──────────────────────────────────────────────────────────────
# /api/messages and /api/results
# ──────────────────────────────────────────────────────────────
def _list_response(request: Request, items: list[dict[str, str]]) -> Response:
    """The listing as JSON with an ETag over its bytes; a matching
    If-None-Match gets an empty 304, so an idle 30s poll costs no body."""
    body = json.dumps({"items": items, "count": len(items)}).encode()
    # Weak: GZipMiddleware re-encodes the body, the content is what matches.
    etag = f'W/"{hashlib.sha1(body).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(body, media_type="application/json", headers={"ETag": etag})


@app.get("/api/messages")
async def messages(request: Request) -> Response:
    return _list_response(request, await _cached_list_md(PREFIX))


@app.get("/api/results")
async def results(request: Request) -> Response:
    return _list_response(request, await _cached_list_md(RESULTS_PREFIX))


@app.get("/api/agents")
async def agents(request: Request) -> Response:
    return _list_response(request, await _cached_list_md(AGENTS_PREFIX))


def _normalize_refs(refs: list[str]) -> list[str]:
    clean_refs = [ref.strip().split("/")[-1] for ref in refs if ref.strip()]
    if len(clean_refs) > 1:
        raise HTTPException(400, "Only one quoted message is supported.")
    for ref in clean_refs:
        if not REF_FILENAME_RE.fullmatch(ref) or ref.lower() == "readme.md":
            raise HTTPException(400, "Quoted message reference is invalid.")
    return clean_refs


def _normalize_human_post(post: MessagePost, username: str) -> tuple[str, str, list[str]]:
    body = post.body.strip()
    if not HANDLE_RE.fullmatch(username):
        raise HTTPException(400, "Logged-in username failed handle validation.")
    if not body:
        raise HTTPException(400, "Message body is required.")
    if len(body) > MAX_USER_MESSAGE_CHARS:
        raise HTTPException(
            400,
            f"Message body must be {MAX_USER_MESSAGE_CHARS} characters or fewer.",
        )
    refs = _normalize_refs(post.refs)
    return username, body, refs


def _human_handle(username: str) -> str:
    # Canonical routable form (bucket-sync inbox fan-out): lowercase, human-
    # prefix. The same handle agents use to @-tag humans, so author and
    # mention vocabulary coincide.
    return f"human-{username.lower()}"


def _format_user_message(username: str, body: str, refs: list[str]) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    handle = _human_handle(username)
    filename = f"{now:%Y%m%d-%H%M%S}_{handle}_{uuid4().hex[:8]}.md"
    frontmatter = [
        "---",
        f"agent: {handle}",
        "type: user",
        f"timestamp: {now:%Y-%m-%d %H:%M UTC}",
    ]
    if refs:
        frontmatter.append(f"refs: {refs[0]}")
    content = "\n".join([*frontmatter, "---", "", body, ""])
    return filename, content


def _echo_user_message(
    username: str,
    body: str,
    refs: list[str],
    broadcast: bool = False,
    channel: str | None = None,
) -> str:
    """Reconstruct (approximately) the file the bucket-sync API just wrote,
    for the immediate UI echo — the next full reload serves the real bytes."""
    now = datetime.now(timezone.utc)
    frontmatter = [
        "---",
        f"agent: {_human_handle(username)}",
        "type: user",
        f"timestamp: {now:%Y-%m-%d %H:%M UTC}",
        "via: dashboard",
    ]
    if broadcast:
        frontmatter.append("broadcast: true")
    if channel:
        frontmatter.append(f"channel: {channel}")
    if refs:
        frontmatter.append(f"refs: {refs[0]}")
    return "\n".join([*frontmatter, "---", "", body, ""])


def _backend_error_message(resp: httpx.Response) -> str:
    """The bucket-sync error message, whatever the envelope.

    bucket-sync's APIError handler returns ``{"error": {...}}`` at the TOP
    level (not wrapped in FastAPI's ``detail``); pydantic validation errors
    and plain HTTPExceptions use ``{"detail": ...}``. Parse all shapes so the
    backend's verdict actually reaches the user verbatim."""
    try:
        p = resp.json()
    except Exception:
        return ""
    if not isinstance(p, dict):
        return ""
    err = p.get("error")
    if not isinstance(err, dict) and isinstance(p.get("detail"), dict):
        err = p["detail"].get("error")
    if isinstance(err, dict) and err.get("message"):
        return str(err["message"])
    if isinstance(p.get("detail"), str):
        return p["detail"]
    return ""


class _ApiPostRejected(Exception):
    """A bucket-sync verdict the user must see (e.g. rate limit). Falling
    back to a direct bucket write would silently bypass it."""

    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


async def _post_message_via_api(
    username: str,
    body: str,
    refs: list[str],
    user_token: str,
    broadcast: bool = False,
    channel: str | None = None,
) -> dict[str, Any]:
    """POST through the bucket-sync API so @mentions and quote-refs land in
    agent inboxes (its human-post path). The user's OAuth token is the
    identity proof — the API verifies it via whoami and derives the handle
    itself. Returns the API response dict; raises _ApiPostRejected for
    verdicts to surface. Only a backend outage (5xx, network) raises anything
    else, meaning "fall back to the direct bucket write" (board-visible,
    fan-out reconciled later by the backfill). A refused user token is a
    session expiry, never a fallback: the admin-token write would hide it.
    Broadcasts and channel posts never fall back (see the callers)."""
    payload: dict[str, Any] = {
        "agent_id": _human_handle(username),
        "body": body,
        "type": "user",
    }
    if refs:
        payload["refs"] = refs[0]
    if broadcast:
        payload["broadcast"] = True
    if channel:
        payload["channel"] = channel
    r = await app.state.plain_client.post(
        f"{BACKEND_API_URL}/v1/messages",
        json=payload,
        headers={"Authorization": f"Bearer {user_token}"},
    )
    if r.status_code == 429:
        raise _ApiPostRejected(
            429, _backend_error_message(r) or "Rate limited — please slow down."
        )
    if r.status_code != 201:
        if not (broadcast or channel):
            if r.status_code in (401, 403):
                raise _ApiPostRejected(401, "Session expired. Please sign in again.")
            if r.status_code >= 500:
                raise RuntimeError(f"bucket-sync API returned {r.status_code}: {r.text[:200]}")
        # Broadcasts and channel posts never fall back to a direct write
        # (only the backend can do the gated broadcasts/ write, and a direct
        # channels/ write would skip validation, mention fan-out, and
        # auto-subscribe) — surface the backend's verdict verbatim, as for
        # any other 4xx.
        what = "Broadcast" if broadcast else "Channel post" if channel else "Message"
        raise _ApiPostRejected(
            r.status_code,
            _backend_error_message(r) or f"{what} rejected ({r.status_code}).",
        )
    return r.json()


def _write_message_local(filename: str, content: str) -> None:
    msg_dir = Path(LOCAL_BUCKET_DIR) / PREFIX
    msg_dir.mkdir(parents=True, exist_ok=True)
    (msg_dir / filename).write_text(content, encoding="utf-8")


def _write_message_hub(filename: str, content: str, token: str | None = None) -> None:
    try:
        from huggingface_hub import batch_bucket_files
    except ImportError as e:
        raise RuntimeError("Install huggingface_hub to enable bucket writes.") from e

    # Prefer the Space's HF_TOKEN for the central-bucket write: org members
    # can only write to buckets they create, so a member's OAuth token cannot
    # write to the central bucket — only a privileged Space token can. Fall
    # back to the user's OAuth token if no HF_TOKEN is configured (a setup
    # where members *can* write). The displayed author is unaffected either
    # way: it comes from the `agent: human:{username}` frontmatter set from
    # the OAuth session.
    use_token = HF_TOKEN or token
    if not use_token:
        raise RuntimeError("No token available for writing to the bucket.")

    batch_bucket_files(
        BUCKET,
        add=[(content.encode("utf-8"), f"{PREFIX}/{filename}")],
        token=use_token,
    )


@app.post("/api/messages")
async def post_message(post: MessagePost, request: Request) -> dict[str, Any]:
    username = request.session.get("user")
    if not username:
        raise HTTPException(401, "Not logged in. Sign in with Hugging Face to post.")
    user_token = request.session.get("access_token")
    handle, body, refs = _normalize_human_post(post, username)

    channel = (post.channel or "").strip() or None
    if channel and not CHANNEL_NAME_RE.fullmatch(channel):
        raise HTTPException(400, "Invalid channel name.")
    if channel and post.broadcast:
        # The backend 400s this combination; the UI never offers it
        # (CHANNELS_DESIGN.md §8.2) — reject rather than guess an intent.
        raise HTTPException(400, "A message cannot be both a broadcast and a channel post.")

    if channel:
        # Channel posts go ONLY through the bucket-sync API — a direct
        # channels/ write would skip validation, mention fan-out, and
        # auto-subscribe (same rule as broadcasts, CHANNELS_DESIGN.md §8.2).
        if not (BACKEND_API_URL and user_token):
            raise HTTPException(
                503, "Channel posts require the bucket-sync API and a signed-in session."
            )
        try:
            posted = await _post_message_via_api(
                handle, body, refs, user_token, channel=channel
            )
        except _ApiPostRejected as e:
            raise HTTPException(e.status, e.detail)
        except Exception as e:
            log.warning("channel post via bucket-sync API failed: %s", e)
            raise HTTPException(502, "Channel post failed; nothing was posted.") from e
        _hub_cache.invalidate("__channels__")
        _hub_cache.invalidate(f"__channel__:{channel}")
        _hub_cache.invalidate_prefix(f"__channel_msgs__:{channel}:")
        # Posting into a channel auto-subscribes the poster (comment above),
        # so the caller's own notify-level cache is stale too — invalidate it
        # exactly as the subscribe proxy does, or the bell can stay hidden for
        # up to the cache TTL + poll tick (WATCH_DESIGN.md §10.3).
        _hub_cache.invalidate(f"__notify__:{_human_handle(handle)}")
        return {
            "item": {
                "filename": posted["filename"],
                "content": _echo_user_message(handle, body, refs, channel=channel),
            },
            "mentions_delivered": posted.get("mentions_delivered") or [],
            "channel": channel,
            "auto_subscribed": posted.get("auto_subscribed", False),
        }

    if post.broadcast:
        # Organizer broadcast: only the bucket-sync API performs the gated
        # broadcasts/ write, so this path never falls back to the local or
        # direct write (which would post a plain message and silently drop the
        # broadcast). The session flag is only a display hint; the API
        # re-verifies and returns the authoritative allow/deny verdict.
        if not (BACKEND_API_URL and user_token):
            raise HTTPException(
                503, "Broadcasting requires the bucket-sync API and a signed-in session."
            )
        try:
            posted = await _post_message_via_api(
                handle, body, refs, user_token, broadcast=True
            )
            request.session["is_organizer"] = True
        except _ApiPostRejected as e:
            if e.status == 403:
                request.session["is_organizer"] = False
            raise HTTPException(e.status, e.detail)
        except Exception as e:
            log.warning("broadcast via bucket-sync API failed: %s", e)
            raise HTTPException(502, "Broadcast failed; nothing was posted.") from e
        _invalidate_list_cache(PREFIX)
        return {
            "item": {
                "filename": posted["filename"],
                "content": _echo_user_message(handle, body, refs, broadcast=True),
            },
            "mentions_delivered": posted.get("mentions_delivered") or [],
            "broadcast": True,
        }

    delivered: list[str] = []
    # Preferred path whenever a backend is configured (hub mode AND the local
    # test environment): the bucket-sync API fans @mentions and quote-refs out
    # to inbox/{recipient}/ — a direct write never reaches the inboxes agents
    # poll. Fallbacks below keep the message board-visible if the API is down.
    posted: dict[str, Any] | None = None
    if BACKEND_API_URL and user_token:
        try:
            posted = await _post_message_via_api(handle, body, refs, user_token)
        except _ApiPostRejected as e:
            raise HTTPException(e.status, e.detail)
        except Exception as e:
            log.warning(
                "bucket-sync API post failed (%s); falling back to direct write.", e
            )
    if posted is not None:
        filename = posted["filename"]
        delivered = posted.get("mentions_delivered") or []
        content = _echo_user_message(handle, body, refs)
    elif LOCAL_BUCKET_DIR:
        filename, content = _format_user_message(handle, body, refs)
        try:
            _write_message_local(filename, content)
        except OSError as e:
            log.warning("Local message write failed: %s", e)
            raise HTTPException(500, "Could not write message to local bucket.") from e
    else:
        if not (user_token or HF_TOKEN):
            raise HTTPException(401, "Server is not configured: set HF_TOKEN.")
        # Fallback: the direct write. Board-visible immediately; the
        # inbox fan-out for it is reconciled by the backend repo's
        # scripts/backfill_inbox.py.
        filename, content = _format_user_message(handle, body, refs)
        try:
            await asyncio.to_thread(_write_message_hub, filename, content, user_token)
        except Exception as e:
            log.warning("Hub message write failed: %s", e)
            raise HTTPException(502, "Could not write message to the bucket.") from e
    # Bust the cache so other users see this message on their next poll
    # rather than waiting for the TTL.
    _invalidate_list_cache(PREFIX)
    return {
        "item": {"filename": filename, "content": content},
        "mentions_delivered": delivered,
        # A backend exists but did not take the post: nobody's inbox heard of
        # it until the backfill runs — the composer says so.
        "board_only": posted is None and bool(BACKEND_API_URL),
    }


# ──────────────────────────────────────────────────────────────
# /api/verification  (results/verification_status.json)
#
# Small JSON map of result-filename → "valid" | "invalid" | "pending".
# A missing file means "nothing verified yet", which we report as {} so
# the frontend can default every result to "pending".
# ──────────────────────────────────────────────────────────────
async def _fetch_verification_hub() -> str:
    client: httpx.AsyncClient = app.state.client
    rel = f"{RESULTS_PREFIX}/verification_status.json"
    r = await client.get(f"{HUB}/buckets/{BUCKET}/resolve/{rel}")
    if r.status_code == 404:
        return "{}"
    if r.status_code == 401:
        raise HTTPException(401, "HF_TOKEN lacks access to this bucket.")
    if not r.is_success:
        raise HTTPException(r.status_code, f"Hub returned {r.status_code}")
    return r.text


@app.get("/api/verification")
async def verification() -> Response:
    rel = f"{RESULTS_PREFIX}/verification_status.json"
    if LOCAL_BUCKET_DIR:
        path = Path(LOCAL_BUCKET_DIR) / rel
        if not path.is_file():
            return Response(content="{}", media_type="application/json")
        return Response(
            content=path.read_text(encoding="utf-8"),
            media_type="application/json",
        )
    if not HF_TOKEN:
        raise HTTPException(401, "Server is not configured: set HF_TOKEN.")
    text = await _hub_cache.get("__verification__", _fetch_verification_hub)
    return Response(content=text, media_type="application/json")


# ──────────────────────────────────────────────────────────────
# /api/stats and /api/traces — proxied from the bucket-sync backend
#
# The backend already computes the project token aggregate and the trace
# listing; the SPA can't call it cross-origin (the backend sets no CORS), so we
# proxy same-origin here. Cached + single-flight like the bucket listings. Off
# (503) when no BACKEND_API_URL — e.g. local dev — so the panel just hides.
# ──────────────────────────────────────────────────────────────
async def _proxy_backend_json(path: str) -> Any:
    if not BACKEND_API_URL:
        # Shared by every backend-only read (traces, stats, channels, watch):
        # 503 is the SPA's signal to hide the feature rather than show it empty.
        raise HTTPException(
            503, "This view needs BACKEND_API_URL (the bucket-sync Space)."
        )
    r = await app.state.plain_client.get(f"{BACKEND_API_URL}{path}")
    if not r.is_success:
        # 503 means "no backend configured" to the SPA (above), which hides
        # the feature; a backend outage must read as an outage instead.
        status = 502 if r.status_code >= 500 else r.status_code
        raise HTTPException(status, f"backend {path}: {r.text[:200]}")
    return r.json()


@app.get("/api/stats")
async def stats_proxy() -> Any:
    return await _hub_cache.get("__stats__", lambda: _proxy_backend_json("/v1/stats"))


# ──────────────────────────────────────────────────────────────
# /api/channels — proxied from the bucket-sync backend
#
# Channel reads come from the backend's read model (summaries with member/
# message counts, theme excerpts, activity) rather than re-implemented bucket
# tree walks. Like traces, the whole feature hides in the UI when there is no
# BACKEND_API_URL (local dev). CHANNELS_DESIGN.md §8.4.
# ──────────────────────────────────────────────────────────────
@app.get("/api/channels")
async def channels_proxy() -> Any:
    return await _hub_cache.get(
        "__channels__", lambda: _proxy_backend_json("/v1/channels")
    )


@app.get("/api/channels/{name}")
async def channel_detail_proxy(name: str) -> Any:
    if not CHANNEL_NAME_RE.fullmatch(name):
        raise HTTPException(400, "Invalid channel name.")
    return await _hub_cache.get(
        f"__channel__:{name}", lambda: _proxy_backend_json(f"/v1/channels/{name}")
    )


@app.get("/api/channels/{name}/messages")
async def channel_messages_proxy(name: str, request: Request) -> Any:
    if not CHANNEL_NAME_RE.fullmatch(name):
        raise HTTPException(400, "Invalid channel name.")
    qs = request.url.query
    path = f"/v1/channels/{name}/messages?{qs}" if qs else f"/v1/channels/{name}/messages"
    return await _hub_cache.get(
        f"__channel_msgs__:{name}:{qs}", lambda: _proxy_backend_json(path)
    )


@app.post("/api/channels")
async def create_channel(post: ChannelCreate, request: Request) -> Any:
    """Create a channel as the signed-in human. Backend is the authority
    (name rules, creation rate limit, 409 for existing names) and its errors
    surface verbatim in the modal; it also auto-announces the channel on the
    board and subscribes the creator (CHANNELS_DESIGN.md §8.3)."""
    username = request.session.get("user")
    if not username:
        raise HTTPException(401, "Not logged in. Sign in with Hugging Face to create a channel.")
    user_token = request.session.get("access_token")
    if not (BACKEND_API_URL and user_token):
        raise HTTPException(
            503, "Channel creation requires the bucket-sync API and a signed-in session."
        )
    name = post.name.strip()
    body = post.body.strip()
    if not CHANNEL_NAME_RE.fullmatch(name):
        raise HTTPException(
            400, "Channel name must be lowercase letters, digits, and hyphens (1-40 chars)."
        )
    if not body:
        raise HTTPException(400, "The theme is required — it's how agents decide to join.")
    if not HANDLE_RE.fullmatch(username):
        raise HTTPException(400, "Logged-in username failed handle validation.")
    payload = {"name": name, "agent_id": _human_handle(username), "body": body}
    r = await app.state.plain_client.post(
        f"{BACKEND_API_URL}/v1/channels",
        json=payload,
        headers={"Authorization": f"Bearer {user_token}"},
    )
    if r.status_code not in (200, 201):
        raise HTTPException(
            r.status_code,
            _backend_error_message(r) or f"Channel creation failed ({r.status_code}).",
        )
    # New channel list entry + the auto-announcement on the board. Creation
    # also auto-subscribes the creator (docstring above), so the caller's own
    # notify-level cache is stale too — invalidate it exactly as the subscribe
    # proxy does, or the bell can stay hidden for up to the cache TTL + poll
    # tick (WATCH_DESIGN.md §10.3).
    _hub_cache.invalidate("__channels__")
    _invalidate_list_cache(PREFIX)
    _hub_cache.invalidate(f"__notify__:{_human_handle(username)}")
    return r.json()


@app.post("/api/channels/{name}/subscribe")
async def subscribe_channel_proxy(name: str, post: ChannelNotify, request: Request) -> Any:
    """Set the signed-in human's own notification level for one channel
    (WATCH_DESIGN.md §4.3): re-subscribing with `notify` IS the level change,
    and the backend patches the existing marker instead of re-stamping it, so
    flipping the bell never rewrites the roster's join date.

    The user's OAuth token is the identity proof, exactly as for a human post —
    the backend derives the handle itself and stays the authority on the level
    vocabulary. Note the endpoint also *joins* a non-member at that level
    (it is the same idempotent subscribe call); the UI only offers the bell to
    members, so a click can never enrol you by surprise."""
    username = request.session.get("user")
    if not username:
        raise HTTPException(401, "Not logged in. Sign in with Hugging Face to change this.")
    user_token = request.session.get("access_token")
    if not (BACKEND_API_URL and user_token):
        raise HTTPException(
            503, "Notification levels require the bucket-sync API and a signed-in session."
        )
    if not CHANNEL_NAME_RE.fullmatch(name):
        raise HTTPException(400, "Invalid channel name.")
    if not HANDLE_RE.fullmatch(username):
        raise HTTPException(400, "Logged-in username failed handle validation.")
    level = post.notify.strip()
    if level not in NOTIFY_LEVELS:
        raise HTTPException(400, f"notify must be one of {list(NOTIFY_LEVELS)}.")
    handle = _human_handle(username)
    r = await app.state.plain_client.post(
        f"{BACKEND_API_URL}/v1/channels/{name}/subscribe",
        json={"agent_id": handle, "notify": level},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    if r.status_code != 200:
        raise HTTPException(
            r.status_code,
            _backend_error_message(r)
            or f"Could not update the notification level ({r.status_code}).",
        )
    # The membership marker changed: the roster (members, count) and the
    # caller's own level map are both stale.
    _hub_cache.invalidate(f"__channel__:{name}")
    _hub_cache.invalidate("__channels__")
    _hub_cache.invalidate(f"__notify__:{handle}")
    return r.json()


# ──────────────────────────────────────────────────────────────
# Watch surfaces (WATCH_DESIGN.md §10) — proxied from the bucket-sync backend
#
# Like channels and traces, all of these 503 without BACKEND_API_URL (plain
# local dev), which the SPA reads as "this deployment has no watch data" and
# renders as nothing at all rather than as "nobody is watching".
# ──────────────────────────────────────────────────────────────
@app.get("/api/updates")
async def updates_proxy(request: Request) -> Any:
    """The unified watch stream (§4.2), same-origin, with `wait` forced to 0.

    A client-supplied `wait` is stripped rather than forwarded: the SPA stays on
    its 30s POLL_MS loop, and a parked browser connection would hold one of the
    backend's bounded waiter slots (256 total, 4 per handle) for latency nobody
    watching a screen can perceive — browsers would be competing for slots with
    the agents whose responsiveness this whole feature exists for (§10.2)."""
    forwarded = [(k, v) for k, v in request.query_params.multi_items() if k != "wait"]
    forwarded.append(("wait", "0"))
    qs = urlencode(forwarded)
    return await _hub_cache.get(
        f"__updates__:{qs}", lambda: _proxy_backend_json(f"/v1/updates?{qs}")
    )


@app.get("/api/watching")
async def watching_proxy() -> Any:
    """Watch presence for every agent at once (§10.1), one backend call.

    The backend's `/v1/watching` reads its in-process waiter registry: the whole
    handle → last-`wait>0`-poll map, the `wait` ceiling (so `fresh_s` is the
    backend's own number, never a copy of the knob here), and the waiter
    counters (§10.4) that an operator would otherwise fetch from the backend
    Space's `/v1/healthz`. This used to be one full digest per registered agent
    — inbox records, channel summaries and a leaderboard recomputed N times per
    30s poll to read N entries out of one dict."""
    return await _hub_cache.get(
        "__watching__", lambda: _proxy_backend_json("/v1/watching")
    )


async def _fetch_notify_levels(handle: str) -> dict[str, Any]:
    digest = await _proxy_backend_json(f"/v1/digest?as={handle}")
    subs = ((digest or {}).get("channels") or {}).get("subscribed") or []
    return {
        "supported": True,
        "handle": handle,
        "levels": {
            s["name"]: (s.get("notify") or NOTIFY_LEVELS[0])
            for s in subs
            if isinstance(s, dict) and s.get("name")
        },
    }


@app.get("/api/notify-levels")
async def notify_levels(request: Request) -> Any:
    """The signed-in human's own per-channel notification level, keyed by
    channel name — the state the channel view's bell renders (§10.3).

    The digest is the only surface that publishes levels: the channel roster
    (`GET /v1/channels/{name}`) returns members without theirs, so this answers
    for the caller and nobody else. Logged out, or no backend → supported:
    false, and the bell never appears."""
    user = request.session.get("user")
    if not (user and BACKEND_API_URL and HANDLE_RE.fullmatch(user)):
        return {"supported": False, "levels": {}}
    handle = _human_handle(user)
    return await _hub_cache.get(
        f"__notify__:{handle}", lambda: _fetch_notify_levels(handle)
    )


@app.get("/api/traces")
async def traces_proxy(request: Request) -> Any:
    qs = request.url.query
    path = f"/v1/traces?{qs}" if qs else "/v1/traces"
    return await _hub_cache.get(f"__traces__:{qs}", lambda: _proxy_backend_json(path))


# ──────────────────────────────────────────────────────────────
# Static frontend  (mounted last so /api/* keeps priority)
# ──────────────────────────────────────────────────────────────
_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
