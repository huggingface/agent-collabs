from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.announce import unique_stamp_time
from app.hub import HubClient
from app.naming import audit_event_path, stamp_iso, utc_now


log = logging.getLogger(__name__)


def event_name(route: str) -> str:
    """Filename-safe event name from a route: /v1/jobs:run -> jobs-run."""
    return re.sub(r"[^a-z0-9]+", "-", route.removeprefix("/v1/").lower()).strip("-")


class AuditLogger:
    """Writes one object per event (``audit/YYYYMM/{stamp}_{event}.json``), so
    a write is O(1) and concurrent writes can't lose each other's records.
    Fire-and-forget: a failed write is logged, never raised into the request."""

    def __init__(self, hub: HubClient):
        self._hub = hub

    def write(
        self,
        *,
        agent_id: str | None,
        route: str,
        via: str | None,
        source: str | None,
        target_path: str | None,
        bytes_count: int,
        status_code: int,
        caller_ip: str | None = None,
        user_agent: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        event = event_name(route)
        # Same per-key monotonic guard as board filenames: two same-ms events
        # on one route still get distinct object names.
        now = unique_stamp_time(f"audit/{event}", utc_now())
        record: dict[str, Any] = {
            "ts": stamp_iso(now),
            "agent_id": agent_id,
            "route": route,
            "via": via,
            "source": source,
            "target_path": target_path,
            "bytes": bytes_count,
            "status_code": status_code,
        }
        if caller_ip is not None:
            record["caller_ip"] = caller_ip
        if user_agent is not None:
            record["user_agent"] = user_agent
        if extra:
            record.update(extra)

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        try:
            self._hub.write_bytes_audit(audit_event_path(event, now), line.encode("utf-8"))
        except Exception as exc:
            log.warning("audit write failed for %s (%s); record=%s", event, exc, record)
