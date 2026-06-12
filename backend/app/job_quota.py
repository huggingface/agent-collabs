"""Durable 24h job quota.

Unlike the in-memory ``TokenBucket`` (which resets on Space restart), this
counts launches against a JSONL ledger persisted in the private audit bucket,
so the per-agent / per-hf_user caps survive restarts and redeploys.

One ledger line per successful launch::

    {"ts": "<iso8601 UTC>", "agent_id": "...", "hf_user": "..."}

``check`` counts launches in the trailing window; ``record`` appends a line and
rewrites the file pruned to the window, so it cannot grow without bound. A
process-wide lock serialises the read-modify-write (the Space runs a single
uvicorn worker; sync endpoints share a threadpool).

``serialized()`` is a coarser lock the launch route holds across its whole
check → launch → record sequence, so two concurrent requests cannot both pass
``check`` before either records. Launches are rare and take seconds; briefly
serialising them is the simple correct fix for that race.

Fail-closed: if the ledger cannot be READ (a storage error, as opposed to a
genuinely missing file), ``check`` raises ``QuotaBackendUnavailable`` so we
never spend org credits on a quota we could not verify. ``record`` failures are
non-fatal — the launch already happened, so we log and report best-effort.
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator

from app.errors import QuotaBackendUnavailable
from app.hub import HubClient
from app.naming import stamp_iso, utc_now


log = logging.getLogger(__name__)


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
        self._launch_lock = threading.Lock()

    @contextmanager
    def serialized(self) -> Iterator[None]:
        """Hold across check → launch → record so concurrent launches cannot
        both pass ``check`` on the same pre-launch ledger state."""
        with self._launch_lock:
            yield

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

    def check(self, agent_id: str, hf_user: str) -> QuotaDecision:
        now = utc_now()
        with self._lock:
            recs = self._load_within_window(now)
        agent_ok = self._count(recs, "agent_id", agent_id) < self._agent_limit
        user_ok = self._count(recs, "hf_user", hf_user) < self._user_limit
        return QuotaDecision(
            agent_ok=agent_ok,
            agent_retry=0 if agent_ok else self._retry_after(recs, "agent_id", agent_id, now),
            user_ok=user_ok,
            user_retry=0 if user_ok else self._retry_after(recs, "hf_user", hf_user, now),
        )

    def record(self, agent_id: str, hf_user: str) -> tuple[int, int]:
        """Commit one launch. Returns (agent_remaining, user_remaining).

        Non-fatal on storage failure: the launch already happened, so we don't
        clobber the ledger (read failed) or fail the response — we log and
        return the full limits as a best-effort, unknown remaining.
        """
        now = utc_now()
        with self._lock:
            try:
                recs = self._load_within_window(now)
            except QuotaBackendUnavailable:
                log.warning(
                    "job-quota ledger unreadable at record time; launch %s/%s not persisted",
                    agent_id, hf_user,
                )
                return self._agent_limit, self._user_limit
            recs.append({"ts": stamp_iso(now), "agent_id": agent_id, "hf_user": hf_user})
            body = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in recs)
            try:
                self._hub.write_bytes_audit(self._path, body.encode("utf-8"))
            except Exception as exc:
                log.warning("job-quota ledger write failed; launch %s/%s not persisted: %s",
                            agent_id, hf_user, exc)
                return self._agent_limit, self._user_limit
        agent_remaining = max(0, self._agent_limit - self._count(recs, "agent_id", agent_id))
        user_remaining = max(0, self._user_limit - self._count(recs, "hf_user", hf_user))
        return agent_remaining, user_remaining
