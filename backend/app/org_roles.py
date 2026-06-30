"""Challenge-org member roles, for the organizer-broadcast gate.

Organizers are the challenge org's ``admin`` members (participants are
``contributor``/``write``). whoami doesn't expose a caller's role for OAuth
tokens, so the Space resolves it with its admin token via
``HubClient.org_member_role_by_email`` when OAuth provides an email, falling
back to ``HubClient.org_member_roles`` when the targeted lookup is unavailable.
Results are cached here: the map changes rarely, and a broadcast is a
deliberate, infrequent act. Lookup failures propagate so the caller can fail
closed.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from app.config import Settings
from app.hub import HubClient


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
        self._email_roles: dict[tuple[str, str], tuple[str | None, float]] = {}
        self._lock = threading.Lock()

    def _current(self) -> dict[str, str]:
        """The cached member→role map, refreshed past the TTL. On a refresh
        failure with no usable cache the underlying error propagates (fail
        closed); a still-fresh cache is served without a fetch."""
        with self._lock:
            now = self._clock()
            if self._roles is None or now - self._fetched_at >= self._settings.org_roles_ttl_s:
                self._roles = self._hub.org_member_roles(self._settings.org)
                self._fetched_at = now
            return self._roles

    def _role_from_email(self, username: str, email: str) -> str | None:
        """Targeted member lookup by OAuth email.

        Returns a role only when the email-filtered member record matches the
        already-verified HF username. A miss is cached briefly but still lets
        the caller fall back to the full org role map.
        """
        username_l = username.lower()
        email_l = email.strip().lower()
        key = (username_l, email_l)
        now = self._clock()
        with self._lock:
            cached = self._email_roles.get(key)
            if cached is not None and now - cached[1] < self._settings.org_roles_ttl_s:
                return cached[0]

        member = self._hub.org_member_role_by_email(self._settings.org, email)
        role = (
            member.role
            if member is not None and member.user.lower() == username_l
            else None
        )
        with self._lock:
            self._email_roles[key] = (role, now)
        return role

    def role_of(self, username: str, email: str | None = None) -> str | None:
        """The caller's role in the challenge org, or None if not a member.
        Raises if the role map can't be fetched."""
        email_error: Exception | None = None
        if email:
            try:
                role = self._role_from_email(username, email)
            except Exception as exc:
                email_error = exc
            else:
                if role is not None:
                    return role
        try:
            return self._current().get(username.lower())
        except Exception:
            if email_error is not None:
                raise email_error
            raise
