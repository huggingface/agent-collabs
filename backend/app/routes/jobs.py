from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request

from app.audit import AuditLogger
from app.auth import HANDSHAKE_FILE, extract_bearer
from app.config import Settings
from app.deps import (
    get_audit,
    get_hub,
    get_job_quota,
    get_job_runner,
    get_settings_dep,
)
from app.errors import (
    BucketNotOwnedByCaller,
    IdentityMismatch,
    JobsDisabled,
    NotRegistered,
    RateLimited,
    Unauthorized,
)
from app.frontmatter import parse
from app.hub import HubClient
from app.job_quota import DurableJobQuota
from app.jobs import JobRunner
from app.models import BenchmarkJobRequest, BenchmarkJobResponse
from app.naming import SourceURI, registration_path
from app.validation import validate_agent_id, validate_path_components


router = APIRouter()


def _registered_hf_user(hub: HubClient, agent_id: str) -> str:
    """Confirm the agent is registered and return its bound hf_user.

    Registration binds agent_id -> hf_user -> agent_bucket, so a present, parseable
    registration is what proves the scratch bucket belongs to this agent.
    """
    try:
        text = hub.read_central_text(registration_path(agent_id))
    except Exception:
        raise NotRegistered(agent_id)
    fm, _ = parse(text)
    hf_user = fm.get("hf_user")
    if not hf_user:
        raise NotRegistered(agent_id)
    return str(hf_user)


def _verify_caller_owns_agent(
    hub: HubClient,
    settings: Settings,
    authorization: str | None,
    agent_id: str,
    registered_hf_user: str,
) -> str:
    """Per-call auth, same proof as POST /v1/agents/register.

    Because a launch spends org credits, identity is proven (not assumed): the
    caller must present a bearer token whose `whoami` matches the hf_user this
    agent is registered to, AND the `.bucket-sync-handshake` in the agent's
    scratch bucket must name that same hf_user (only the bucket creator could
    have written it). A bystander who merely knows the agent_id cannot forge it.
    """
    token = extract_bearer(authorization)
    if not token:
        raise Unauthorized(
            "missing Authorization: Bearer <hf_token>",
            hint="pass your HF token so we can verify you own this agent",
        )
    try:
        caller_hf_user = hub.whoami_for_token(token)
    except Exception:
        raise Unauthorized("could not resolve caller identity via whoami; check your token")

    handshake_uri = SourceURI(
        org=settings.org,
        bucket=f"{settings.collab_slug}-{agent_id}",
        path=HANDSHAKE_FILE,
    )
    try:
        handshake_content = hub.read_text(handshake_uri).strip()
    except FileNotFoundError:
        raise BucketNotOwnedByCaller(
            "handshake file missing in scratch bucket",
            hint=(
                f"echo '{caller_hf_user}' > /tmp/h && "
                f"hf buckets cp /tmp/h hf://buckets/{settings.agent_bucket(agent_id)}/{HANDSHAKE_FILE}"
            ),
        )
    if handshake_content != caller_hf_user:
        raise BucketNotOwnedByCaller(
            f"handshake content '{handshake_content}' does not match caller hf_user '{caller_hf_user}'",
        )
    if caller_hf_user != registered_hf_user:
        raise IdentityMismatch(
            f"agent '{agent_id}' is registered to '{registered_hf_user}', not caller '{caller_hf_user}'"
        )
    return caller_hf_user


@router.post("/v1/jobs:run", response_model=BenchmarkJobResponse, status_code=202)
def run_benchmark_job(
    req: BenchmarkJobRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    hub: HubClient = Depends(get_hub),
    runner: JobRunner = Depends(get_job_runner),
    audit: AuditLogger = Depends(get_audit),
    quota: DurableJobQuota = Depends(get_job_quota),
) -> BenchmarkJobResponse:
    if not settings.jobs_enabled:
        raise JobsDisabled()
    validate_agent_id(req.agent_id)
    validate_path_components(req.submission_prefix)
    validate_path_components(req.run_prefix)

    hf_user = _registered_hf_user(hub, req.agent_id)
    _verify_caller_owns_agent(hub, settings, authorization, req.agent_id, hf_user)

    bucket = settings.agent_bucket(req.agent_id)
    submission_prefix = req.submission_prefix.strip("/")
    run_prefix = req.run_prefix.strip("/")

    # We deliberately do NOT pre-check the submission contents: the in-job
    # harness validates them and fails fast, and its logs land in
    # run_prefix/job_logs.txt for the participant to debug.

    # check → launch → record runs under one lock so two concurrent requests
    # cannot both pass `check` on the same pre-launch ledger state. The quota
    # is durable (a ledger in the private audit bucket), so the counts survive
    # Space restarts.
    with quota.serialized():
        decision = quota.check(req.agent_id, hf_user)
        if not decision.agent_ok:
            raise RateLimited(
                decision.agent_retry,
                f"agent '{req.agent_id}' has hit its limit of "
                f"{settings.job_per_agent_per_day} jobs per 24h; retry after {decision.agent_retry}s",
            )
        if not decision.user_ok:
            raise RateLimited(
                decision.user_retry,
                f"hf_user '{hf_user}' has hit its limit of "
                f"{settings.job_per_user_per_day} jobs per 24h; retry after {decision.user_retry}s",
            )

        job_id, job_url = runner.launch_benchmark(
            agent_id=req.agent_id,
            hf_user=hf_user,
            bucket=bucket,
            submission_prefix=submission_prefix,
            run_prefix=run_prefix,
        )

        # Count the launch against both windows only after it actually succeeded.
        agent_remaining, user_remaining = quota.record(req.agent_id, hf_user)

    status_file = f"hf://buckets/{bucket}/{run_prefix}/job_status.json"
    logs_file = f"hf://buckets/{bucket}/{run_prefix}/job_logs.txt"

    audit.write(
        agent_id=req.agent_id,
        route="/v1/jobs:run",
        via="job",
        source=f"hf://buckets/{bucket}/{submission_prefix}",
        target_path=run_prefix,
        bytes_count=0,
        status_code=202,
        caller_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        extra={"job_id": job_id, "hf_user": hf_user},
    )

    return BenchmarkJobResponse(
        agent_id=req.agent_id,
        hf_user=hf_user,
        submission_bucket=bucket,
        submission_prefix=submission_prefix,
        run_bucket=bucket,
        run_prefix=run_prefix,
        job_id=job_id,
        job_url=job_url,
        status="launched",
        timeout_minutes=settings.job_timeout_minutes,
        status_file=status_file,
        logs_file=logs_file,
        quota={
            "agent_remaining": agent_remaining,
            "user_remaining": user_remaining,
        },
        message=(
            f"benchmark launched (capped at {settings.job_timeout_minutes} min). "
            "Poll job_status.json / job_logs.txt in your run_prefix, or view the job "
            "directly at job_url; you can read it but cannot manage it."
        ),
    )
