"""Challenge-org member roles, for the organizer gate (broadcasts, channel
creation).

Organizers are the challenge org's ``admin`` members (participants are
``contributor``/``write``). The role comes from the first Hub source that
states it, never from the caller:

1. the caller's own whoami (``HubIdentity.roles``): tokens scoped to the
   org carry ``roleInOrg``;
2. ``/oauth/userinfo`` with the caller's token, for OAuth tokens whose
   whoami may omit it;
3. the org members listing read with the Space's token, cached for
   ``ORG_ROLES_TTL_S``. The Hub includes roles there only for tokens allowed
   to see them.

When no source states a role the answer is ``RoleUnknown``, not "not an
admin". Outages propagate so the caller fails closed.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from app.config import Settings
from app.hub import HubClient, HubIdentity, RolesNotExposed


class RoleUnknown(Exception):
    """No Hub source states the caller's role: their token doesn't expose it
    and the Space's token can't see member roles."""


class OrgRoles:
    def __init__(
        self,
        hub: HubClient,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._hub = hub
        self._settings = settings
        self._clock = clock
        self._roles: dict[str, str] | None = None
        self._fetched_at = float("-inf")
        self._lock = threading.Lock()

    def _member_roles(self) -> dict[str, str]:
        """The members listing's role map, refetched past the TTL. Only a
        complete listing with roles is cached; a failure caches nothing and
        propagates."""
        with self._lock:
            now = self._clock()
            if self._roles is None or now - self._fetched_at >= self._settings.org_roles_ttl_s:
                self._roles = self._hub.org_member_roles(self._settings.org)
                self._fetched_at = now
            return self._roles

    def role_of(self, identity: HubIdentity, token: str) -> str | None:
        """The caller's role in the challenge org, or None when a complete
        role listing doesn't include them. Raises RoleUnknown when no source
        states roles, and the outage error when a source that might have
        answered was unreachable."""
        org = self._settings.org
        role = identity.roles.get(org)
        if role is not None:
            return role
        outage: Exception | None = None
        try:
            role = self._hub.oauth_org_role(token, identity.username, org)
        except Exception as exc:
            outage = exc
        else:
            if role is not None:
                return role
        try:
            return self._member_roles().get(identity.username.lower())
        except RolesNotExposed:
            if outage is not None:
                raise outage
            raise RoleUnknown() from None
