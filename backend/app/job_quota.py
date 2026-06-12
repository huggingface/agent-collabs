"""Durable 24h job quota.

Unlike the in-memory ``SlidingWindowLimiter`` (which resets on Space restart),
this counts launches against a JSONL ledger persisted in the private audit
bucket, so the per-agent / per-hf_user caps survive restarts and redeploys.

One ledger line per successful launch::

    {"ts": "<iso8601 UTC>", "agent_id": "...", "hf_user": "..."}

``launch_within_quota`` is the single entry point: under one process-wide lock
it counts launches in the trailing window, runs the launch if both caps have
room, then appends a line and rewrites the file pruned to the window (so it
cannot grow without bound). Holding the lock across the whole check -> launch
-> record sequence means two concurrent requests cannot both pass the check
against the same pre-launch ledger state and overshoot the caps; launches are
rare, so serialising them is fine. (The Space runs a single uvicorn worker;
sync endpoints share a threadpool.)

Fail-closed: if the ledger cannot be READ (a storage error, as opposed to a
genuinely missing file), we raise ``QuotaBackendUnavailable`` so we never
spend org credits on a quota we could not verify. Ledger-write failures are
non-fatal — the launch already happened, so we log and report best-effort.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from app.errors import QuotaBackendUnavailable
from app.hub import HubClient
from app.naming import stamp_iso, utc_now


log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class QuotaDecision:
    agent_ok: bool
    agent_retry: int
    user_ok: bool
    user_retry: int


def _parse_ts(ts: str) -> datetime:
    # Ledger timestamps are ISO8601 UTC with a trailing 'Z' (see stamp_iso).
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class DurableJobQuota:
    def __init__(
        self,
        hub: HubClient,
        path: str,
        agent_limit: int,
        user_limit: int,
        window_seconds: float,
    ):
        self._hub = hub
        self._path = path
        self._agent_limit = agent_limit
        self._user_limit = user_limit
        self._window = timedelta(seconds=window_seconds)
        self._lock = threading.Lock()

    def _load_within_window(self, now: datetime) -> list[dict]:
        """Ledger records within the trailing window. Raises (fail-closed) on a
        storage error; an empty/missing ledger is an empty list, not an error."""
        try:
            raw = self._hub.read_audit_bytes(self._path)
        except Exception as exc:
            log.warning("job-quota ledger read failed: %s", exc)
            raise QuotaBackendUnavailable()
        if not raw:
            return []
        cutoff = now - self._window
        out: list[dict] = []
        for line in raw.decode("utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if _parse_ts(rec["ts"]) > cutoff:
                    out.append(rec)
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                log.warning("skipping bad job-ledger line (%s): %s", exc, line[:120])
        return out

    @staticmethod
    def _count(recs: list[dict], field: str, key: str) -> int:
        return sum(1 for r in recs if r.get(field) == key)

    def _retry_after(self, recs: list[dict], field: str, key: str, now: datetime) -> int:
        """Seconds until this key's OLDEST in-window launch ages out."""
        times = sorted(_parse_ts(r["ts"]) for r in recs if r.get(field) == key)
        if not times:
            return 1
        ages_out = times[0] + self._window
        return max(1, int((ages_out - now).total_seconds()) + 1)

    def launch_within_quota(
        self,
        agent_id: str,
        hf_user: str,
        launch: Callable[[], T],
    ) -> tuple[QuotaDecision, T | None, int, int]:
        """Check both caps, run ``launch``, and record it — under one lock.

        Returns ``(decision, result, agent_remaining, user_remaining)``.
        ``result`` is whatever ``launch()`` returned, or None when the decision
        rejects (``launch`` is never called). If ``launch`` raises, nothing is
        recorded and the exception propagates.

        Raises ``QuotaBackendUnavailable`` if the ledger cannot be read
        (fail-closed, before launching). A failed ledger WRITE is non-fatal:
        the launch already happened, so we log and return the full limits as a
        best-effort, unknown remaining.
        """
        now = utc_now()
        with self._lock:
            recs = self._load_within_window(now)
            agent_used = self._count(recs, "agent_id", agent_id)
            user_used = self._count(recs, "hf_user", hf_user)
            agent_ok = agent_used < self._agent_limit
            user_ok = user_used < self._user_limit
            decision = QuotaDecision(
                agent_ok=agent_ok,
                agent_retry=0 if agent_ok else self._retry_after(recs, "agent_id", agent_id, now),
                user_ok=user_ok,
                user_retry=0 if user_ok else self._retry_after(recs, "hf_user", hf_user, now),
            )
            if not (agent_ok and user_ok):
                return (
                    decision,
                    None,
                    max(0, self._agent_limit - agent_used),
                    max(0, self._user_limit - user_used),
                )
            result = launch()
            recs.append({"ts": stamp_iso(utc_now()), "agent_id": agent_id, "hf_user": hf_user})
            body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)
            try:
                self._hub.write_bytes_audit(self._path, body.encode("utf-8"))
            except Exception as exc:
                log.warning("job-quota ledger write failed; launch %s/%s not persisted: %s",
                            agent_id, hf_user, exc)
                return decision, result, self._agent_limit, self._user_limit
        agent_remaining = max(0, self._agent_limit - agent_used - 1)
        user_remaining = max(0, self._user_limit - user_used - 1)
        return decision, result, agent_remaining, user_remaining
