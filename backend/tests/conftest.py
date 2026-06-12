from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.audit import AuditLogger
from app.config import Settings
from app.dedup import PromotionLRU
from app.deps import (
    get_audit,
    get_bucket_write_limiter,
    get_dedup,
    get_hub,
    get_raw_message_limiter,
    get_read_model,
    get_registration_limiter,
    get_settings_dep,
    get_verification_status,
    get_verifier,
)
from app.main import app as fastapi_app
from app.rate_limit import CompoundLimiter, TokenBucket
from app.read_model import ReadModel
from app.verification import VerificationStatusStore
from app.verifier import Verifier
from fakes import FakeHub, FakeJobRunner


@pytest.fixture
def make_env():
    """Build an app environment around a FakeHub, with optional Settings
    overrides (passed by env-var alias, e.g. MENTION_FANOUT_CAP=2)."""

    def _make(**settings_overrides):
        settings = Settings(
            HF_TOKEN="test-token",
            ORG="test-org",
            COLLAB_SLUG="test",
            AUDIT_BUCKET="auditor/test-audit",
            VERIFIER_AGENT="test-verifier",
            **settings_overrides,
        )
        hub = FakeHub(settings)
        read_model = ReadModel(hub, settings)
        # One LRU per env, like production's process-wide singleton — a
        # per-request instance would make cross-request dedup untestable.
        dedup = PromotionLRU(10_000)
        verification = VerificationStatusStore(
            hub, runs_prefix=settings.verification_runs_prefix
        )
        runner = FakeJobRunner()
        # Inline spawn: the verdict watcher runs synchronously inside the POST,
        # so tests assert final state without thread coordination.
        verifier = Verifier(
            settings, hub, read_model, verification, runner,
            spawn=lambda _name, fn: fn(),
        )
        generous = lambda: CompoundLimiter(  # noqa: E731 — tests never rate limit
            TokenBucket(capacity=1000, refill_per_minute=1000),
            TokenBucket(capacity=1000, refill_per_minute=1000),
        )
        fastapi_app.dependency_overrides.update(
            {
                get_settings_dep: lambda: settings,
                get_hub: lambda: hub,
                get_read_model: lambda: read_model,
                get_audit: lambda: AuditLogger(hub),
                get_dedup: lambda: dedup,
                get_verification_status: lambda: verification,
                get_verifier: lambda: verifier,
                get_bucket_write_limiter: generous,
                get_raw_message_limiter: generous,
                get_registration_limiter: lambda: TokenBucket(
                    capacity=1000, refill_per_minute=1000
                ),
            }
        )
        return SimpleNamespace(
            settings=settings,
            hub=hub,
            read_model=read_model,
            verification=verification,
            runner=runner,
            verifier=verifier,
            client=TestClient(fastapi_app),
        )

    yield _make
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def env(make_env):
    return make_env()
