from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header

from app.auth import extract_bearer
from app.config import Settings
from app.deps import get_hub, get_org_roles, get_read_model, get_settings_dep
from app.errors import Unauthorized
from app.hub import HubClient
from app.models import AgentTraces, MeResponse
from app.org_roles import OrgRoles
from app.read_model import ReadModel
from app.trace_stats import agent_traces
from app.validation import HUMAN_HANDLE_PREFIX


log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/v1/me", response_model=MeResponse)
def get_me(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    org_roles: OrgRoles = Depends(get_org_roles),
    read_model: ReadModel = Depends(get_read_model),
) -> MeResponse:
    """Who the bearer token belongs to, and whether they may broadcast.

    The dashboard calls this with the signed-in user's OAuth token to decide
    whether to show the organizer-only broadcast toggle. It is a UI hint, not
    the security boundary — POST /v1/messages re-verifies the role on every
    broadcast. The organizer check reuses the cached org-role lookup and
    degrades to is_organizer=false if that lookup is unavailable, so a
    transient outage hides the toggle rather than 503-ing the whole page.

    `traces` sums what this user's registered agents have shared. It is
    best-effort the same way: if the agents or traces folder can't be read,
    it is null ("unknown right now") rather than an error for the whole
    response — never a zero, which would claim nothing was shared.
    """
    token = extract_bearer(authorization)
    if not token:
        raise Unauthorized(
            "GET /v1/me requires Authorization: Bearer <hf_token>",
            hint="the dashboard forwards the signed-in user's OAuth token",
        )
    try:
        identity = hub.whoami_identity(token)
    except Exception:
        raise Unauthorized(
            "could not resolve caller identity via whoami; check your token"
        )
    is_member = settings.org in identity.orgs
    is_organizer = False
    if is_member:
        try:
            is_organizer = (
                org_roles.role_of(identity.username, email=identity.email) == "admin"
            )
        except Exception:
            is_organizer = False
    return MeResponse(
        hf_user=identity.username,
        handle=f"{HUMAN_HANDLE_PREFIX}{identity.username.lower()}",
        is_member=is_member,
        is_organizer=is_organizer,
        traces=_my_traces(read_model, identity.username),
    )


def _my_traces(read_model: ReadModel, hf_user: str) -> AgentTraces | None:
    """Traces shared by the agents `hf_user` registered; None if the agents or
    traces folder can't be read (logged, type only)."""
    try:
        my_agents = {
            r.filename.removesuffix(".md")
            for r in read_model.records("agents")
            if r.frontmatter.get("hf_user") == hf_user
        }
        return agent_traces(read_model, my_agents)
    except Exception as e:
        log.warning("trace summary unavailable for /v1/me (type=%s)", type(e).__name__)
        return None
