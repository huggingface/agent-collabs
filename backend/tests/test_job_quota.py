"""DurableJobQuota: the check -> launch -> record sequence is atomic."""
from __future__ import annotations

import threading
import time

import pytest

from app.config import Settings
from app.errors import QuotaBackendUnavailable
from app.job_quota import DurableJobQuota
from fakes import FakeHub

LEDGER = "jobs/quota.jsonl"


def make_quota(agent_limit: int = 1, user_limit: int = 30) -> tuple[DurableJobQuota, FakeHub]:
    settings = Settings(
        HF_TOKEN="test-token",
        ORG="test-org",
        COLLAB_SLUG="test",
        AUDIT_BUCKET="auditor/test-audit",
    )
    hub = FakeHub(settings)
    quota = DurableJobQuota(
        hub,
        path=LEDGER,
        agent_limit=agent_limit,
        user_limit=user_limit,
        window_seconds=86400,
    )
    return quota, hub


def test_concurrent_launches_cannot_overshoot_the_cap():
    quota, _ = make_quota(agent_limit=1)
    launches: list[int] = []
    started = threading.Barrier(2)

    def launch():
        launches.append(threading.get_ident())
        time.sleep(0.02)  # widen the old check->record race window
        return ("job-1", "https://hf.co/jobs/job-1")

    results = []

    def attempt():
        started.wait()
        results.append(quota.launch_within_quota("agent-a", "user-a", launch))

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one launch went through; the loser saw the winner's record.
    assert len(launches) == 1
    granted = [r for r in results if r[1] is not None]
    rejected = [r for r in results if r[1] is None]
    assert len(granted) == 1 and len(rejected) == 1
    decision = rejected[0][0]
    assert decision.agent_ok is False
    assert decision.agent_retry >= 1


def test_rejected_launch_is_never_called():
    quota, _ = make_quota(agent_limit=1)
    quota.launch_within_quota("agent-a", "user-a", lambda: "first")

    decision, result, agent_remaining, _ = quota.launch_within_quota(
        "agent-a", "user-a", lambda: pytest.fail("launch must not run when over quota")
    )
    assert decision.agent_ok is False
    assert result is None
    assert agent_remaining == 0


def test_failed_launch_is_not_counted():
    quota, hub = make_quota(agent_limit=1)

    def boom():
        raise RuntimeError("hub jobs API down")

    with pytest.raises(RuntimeError):
        quota.launch_within_quota("agent-a", "user-a", boom)
    assert hub.read_audit_bytes(LEDGER) is None

    decision, result, agent_remaining, user_remaining = quota.launch_within_quota(
        "agent-a", "user-a", lambda: "ok"
    )
    assert decision.agent_ok is True
    assert result == "ok"
    assert agent_remaining == 0
    assert user_remaining == 29


def test_unreadable_ledger_fails_closed_before_launching():
    quota, hub = make_quota()

    def broken_read(path):
        raise RuntimeError("storage down")

    hub.read_audit_bytes = broken_read

    with pytest.raises(QuotaBackendUnavailable):
        quota.launch_within_quota(
            "agent-a", "user-a", lambda: pytest.fail("must not launch unverified")
        )
