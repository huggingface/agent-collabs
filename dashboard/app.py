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
BACKEND_API_URL = os.environ.get("BACKEND_API_URL", "")

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
OAUTH_SCOPES = os.environ.get("OAUTH_SCOPES", "openid profile write-repos")
OAUTH_REQUIRED_ORG = os.environ.get("OAUTH_REQUIRED_ORG", ORG)
SESSION_SECRET = (
    os.environ.get("SESSION_SECRET")
    or os.environ.get("OAUTH_CLIENT_SECRET")  # stable across restarts on HF
    or secrets.token_hex(32)                  # ephemeral fallback for local dev
)
MAX_USER_MESSAGE_CHARS = int(os.environ.get("MAX_USER_MESSAGE_CHARS", "4000"))
HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
REF_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.md$")


class MessagePost(BaseModel):
    body: str = ""
    refs: list[str] = Field(default_factory=list)


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


# ──────────────────────────────────────────────────────────────
# Health & config
# ──────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health() -> dict[str, Any]:
    mode = "local" if LOCAL_BUCKET_DIR else ("hub" if HF_TOKEN else "unconfigured")
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

    # Use a fresh client so we don't inherit `Authorization: Bearer HF_TOKEN`
    # from app.state.client — HF's /oauth/token expects client_id+client_secret,
    # not a Space-token Bearer header, and rejects the request otherwise.
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(HUB_FETCH_TIMEOUT), follow_redirects=True) as oauth_client:
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


@app.get("/api/me")
async def api_me(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not user:
        return {"logged_in": False, "oauth_configured": bool(OAUTH_CLIENT_ID)}
    return {
        "logged_in": True,
        "user": user,
        "avatar": request.session.get("avatar") or "",
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

    tree_resp = await client.get(f"{HUB}/api/buckets/{BUCKET}/tree/{prefix}")
    if tree_resp.status_code == 404:
        # Folder may not exist yet (e.g. fresh `results/` before any agent posts).
        return []
    if tree_resp.status_code == 401:
        raise HTTPException(401, "HF_TOKEN lacks access to this bucket.")
    if not tree_resp.is_success:
        raise HTTPException(tree_resp.status_code, f"Hub tree fetch: {tree_resp.text[:200]}")

    entries: list[dict[str, Any]] = [
        e
        for e in tree_resp.json()
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
        value = await refresh()
        self._values[key] = (time.monotonic(), value)
        return value

    def invalidate(self, key: str) -> None:
        self._values.pop(key, None)


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
@app.get("/api/messages")
async def messages() -> dict[str, Any]:
    items = await _cached_list_md(PREFIX)
    return {"items": items, "count": len(items)}


@app.get("/api/results")
async def results() -> dict[str, Any]:
    items = await _cached_list_md(RESULTS_PREFIX)
    return {"items": items, "count": len(items)}


@app.get("/api/agents")
async def agents() -> dict[str, Any]:
    items = await _cached_list_md(AGENTS_PREFIX)
    return {"items": items, "count": len(items)}


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


def _format_user_message(username: str, body: str, refs: list[str]) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    filename = f"{now:%Y%m%d-%H%M%S}_human-{username}_{uuid4().hex[:8]}.md"
    frontmatter = [
        "---",
        f"agent: human:{username}",
        "type: user",
        f"timestamp: {now:%Y-%m-%d %H:%M UTC}",
    ]
    if refs:
        frontmatter.append(f"refs: {refs[0]}")
    content = "\n".join([*frontmatter, "---", "", body, ""])
    return filename, content


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
    filename, content = _format_user_message(handle, body, refs)
    if LOCAL_BUCKET_DIR:
        try:
            _write_message_local(filename, content)
        except OSError as e:
            log.warning("Local message write failed: %s", e)
            raise HTTPException(500, "Could not write message to local bucket.") from e
    else:
        # The hub write needs a token; prefer the user's OAuth token so the
        # commit is attributed to them, falling back to HF_TOKEN.
        if not (user_token or HF_TOKEN):
            raise HTTPException(401, "Server is not configured: set HF_TOKEN.")
        try:
            await asyncio.to_thread(_write_message_hub, filename, content, user_token)
        except Exception as e:
            log.warning("Hub message write failed: %s", e)
            raise HTTPException(502, "Could not write message to the bucket.") from e
    # Bust the cache so other users see this message on their next poll
    # rather than waiting for the TTL.
    _invalidate_list_cache(PREFIX)
    return {"item": {"filename": filename, "content": content}}


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
# Static frontend  (mounted last so /api/* keeps priority)
# ──────────────────────────────────────────────────────────────
_static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")
