from __future__ import annotations

from functools import lru_cache

from app.audit import AuditLogger
from app.config import Settings, get_settings
from app.dedup import PromotionLRU
from app.hub import HubClient
from app.job_quota import DurableJobQuota
from app.jobs import JobRunner
from app.notify import Notifier
from app.org_roles import OrgRoles
from app.rate_limit import CompoundLimiter, TokenBucket
from app.read_model import ReadModel
from app.verification import VerificationStatusStore
from app.verifier import Verifier


_DAY_SECONDS = 24 * 60 * 60


@lru_cache
def get_hub() -> HubClient:
    return HubClient(get_settings())


@lru_cache
def get_job_runner() -> JobRunner:
    return JobRunner(get_settings(), get_hub())


@lru_cache
def get_job_quota() -> DurableJobQuota:
    s = get_settings()
    return DurableJobQuota(
        hub=get_hub(),
        path=s.job_quota_ledger_path,
        agent_limit=s.job_per_agent_per_day,
        user_limit=s.job_per_user_per_day,
        window_seconds=_DAY_SECONDS,
    )


@lru_cache
def get_read_model() -> ReadModel:
    return ReadModel(get_hub(), get_settings())


@lru_cache
def get_notifier() -> Notifier:
    """The one in-process long-poll waiter registry (WATCH_DESIGN.md §3). It is
    per-process by design, which is why the Dockerfile pins `--workers 1`: with
    more workers a writer would wake only the waiters that happen to share its
    worker and every other `wait=` would silently time out."""
    s = get_settings()
    return Notifier(
        max_waiters_per_owner=s.longpoll_max_waiters_per_owner,
        max_waiters_total=s.longpoll_max_waiters_total,
        wake_spread_s=s.longpoll_wake_spread_s,
        wake_spread_threshold=s.longpoll_wake_spread_threshold,
    )


@lru_cache
def get_audit() -> AuditLogger:
    return AuditLogger(get_hub())


@lru_cache
def get_org_roles() -> OrgRoles:
    return OrgRoles(get_hub(), get_settings())


@lru_cache
def get_dedup() -> PromotionLRU:
    return PromotionLRU(max_entries=get_settings().dedup_lru_size)


@lru_cache
def get_verification_status() -> VerificationStatusStore:
    return VerificationStatusStore(
        get_hub(), runs_prefix=get_settings().verification_runs_prefix
    )


@lru_cache
def get_verifier() -> Verifier:
    return Verifier(
        get_settings(),
        get_hub(),
        get_read_model(),
        get_verification_status(),
        get_job_runner(),
        notifier=get_notifier(),
    )


@lru_cache
def get_bucket_write_limiter() -> CompoundLimiter:
    s = get_settings()
    burst = TokenBucket(capacity=s.bucket_write_burst, refill_per_minute=s.bucket_write_burst)
    sustained = TokenBucket(capacity=s.bucket_write_per_minute, refill_per_minute=s.bucket_write_per_minute)
    return CompoundLimiter(burst, sustained)


@lru_cache
def get_raw_message_limiter() -> CompoundLimiter:
    s = get_settings()
    per_minute = TokenBucket(capacity=s.raw_message_per_minute, refill_per_minute=s.raw_message_per_minute)
    per_hour = TokenBucket(capacity=s.raw_message_per_hour, refill_per_minute=max(1, s.raw_message_per_hour // 60))
    return CompoundLimiter(per_minute, per_hour)


@lru_cache
def get_registration_limiter() -> TokenBucket:
    s = get_settings()
    return TokenBucket(capacity=s.registration_per_minute, refill_per_minute=s.registration_per_minute)


def get_settings_dep() -> Settings:
    return get_settings()
