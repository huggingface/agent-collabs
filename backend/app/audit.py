from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.hub import HubClient
from app.naming import audit_log_path, stamp_iso, utc_now


log = logging.getLogger(__name__)


class AuditLogger:
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
        now = utc_now()
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

        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        path = audit_log_path(now)
        try:
            self._hub.append_jsonl_audit(path, line)
        except Exception:
            log.exception("audit append failed; record=%s", record)
