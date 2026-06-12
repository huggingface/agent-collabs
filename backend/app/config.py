"""Per-challenge configuration.

Everything that distinguishes one challenge from another arrives through
environment variables — on a deployed Space they are written by
``bootstrap/init_challenge.py`` from the repo's ``challenge.yaml``. The app
itself never reads challenge.yaml, so a running Space can be reconfigured by
editing its variables alone.
"""
import json
from functools import lru_cache
from typing import Literal

from huggingface_hub import get_token
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Collaboration identity (required, no defaults — fail loud) ──
    org: str = Field(alias="ORG")
    collab_slug: str = Field(alias="COLLAB_SLUG")
    # Default derived as {org}/{collab_slug}-main-bucket (see validator).
    central_bucket: str = Field("", alias="CENTRAL_BUCKET")
    # Private bucket for the audit log and quota ledger; the Space is its only
    # writer. Defaults into the org ({org}/{slug}-audit, set by bootstrap);
    # place it OUTSIDE the org (personal account) when members must not be
    # able to read it — audit rows carry caller_ip/user_agent, and the
    # verifier's private eval set lives here.
    audit_bucket: str = Field(alias="AUDIT_BUCKET")
    # Durable 24h job-quota ledger, stored in the private audit bucket under a
    # separate prefix (decoupled from the audit log so purges don't reset
    # quotas). Lets the per-agent / per-user job caps survive Space restarts.
    job_quota_ledger_path: str = Field(
        "quota/job_ledger.jsonl", alias="JOB_QUOTA_LEDGER_PATH"
    )

    # Admin token: writes the central bucket, appends the audit log, and (if
    # jobs are enabled) launches benchmark jobs on org credits. It is ONLY a
    # launch credential — never injected into a job container.
    hf_token: str | None = Field(None, alias="HF_TOKEN")

    # ── Scoring (what makes a result a result) ──
    # The numeric frontmatter field results are ranked on.
    score_field: str = Field("score", alias="SCORE_FIELD")
    # Human-readable unit for docs and the API self-description.
    score_unit: str = Field("points", alias="SCORE_UNIT")
    # desc = higher is better, asc = lower is better.
    score_order: Literal["desc", "asc"] = Field("desc", alias="SCORE_ORDER")
    # CSV of required result-frontmatter fields. The score field is always
    # required and validated as a positive number; `status` is always
    # validated against agent-run|negative when present in this list.
    required_result_fields: str = Field(
        "score,method,status,description", alias="REQUIRED_RESULT_FIELDS"
    )

    sync_max_bytes: int = Field(5 * 1024**3, alias="SYNC_MAX_BYTES")
    sync_max_files: int = Field(10_000, alias="SYNC_MAX_FILES")

    bucket_write_per_minute: int = Field(60, alias="BUCKET_WRITE_PER_MINUTE")
    bucket_write_burst: int = Field(20, alias="BUCKET_WRITE_BURST")
    raw_message_per_minute: int = Field(5, alias="RAW_MESSAGE_PER_MINUTE")
    raw_message_per_hour: int = Field(30, alias="RAW_MESSAGE_PER_HOUR")
    registration_per_minute: int = Field(3, alias="REGISTRATION_PER_MINUTE")

    dedup_lru_size: int = Field(10_000, alias="DEDUP_LRU_SIZE")

    # Read model & discovery endpoints. The listing TTL bounds staleness for
    # out-of-band admin edits only — API writes are cached write-through.
    listing_ttl_s: float = Field(30.0, alias="LISTING_TTL_S")
    content_cache_max_bytes: int = Field(64 * 1024**2, alias="CONTENT_CACHE_MAX_BYTES")
    expand_max_limit: int = Field(200, alias="EXPAND_MAX_LIMIT")
    mention_fanout_cap: int = Field(10, alias="MENTION_FANOUT_CAP")

    # ── Benchmark jobs (optional; POST /v1/jobs:run is 404 when off) ──
    jobs_enabled: bool = Field(False, alias="JOBS_ENABLED")
    # Harness contract: a directory at {central_bucket}/{harness_prefix}
    # containing {harness_entrypoint}. The job runs
    #   python3 /harness/{entrypoint} --submission-dir /submission
    #       --state-dir /state [--private-dir /private] {extra args}
    # and must write /state/summary.json with at least {score_field: number}.
    harness_prefix: str = Field("shared_resources/harness", alias="HARNESS_PREFIX")
    harness_entrypoint: str = Field("run.py", alias="JOB_HARNESS_ENTRYPOINT")
    # JSON list of extra CLI args appended to the harness command.
    job_extra_args: str = Field("[]", alias="JOB_EXTRA_ARGS")
    job_image: str = Field("python:3.12", alias="JOB_IMAGE")
    job_flavor: str = Field("a10g-small", alias="JOB_FLAVOR")
    job_timeout_minutes: int = Field(40, alias="JOB_TIMEOUT_MINUTES")
    # How long the watcher polls past the platform cap before forcing a cancel.
    job_watch_poll_s: int = Field(20, alias="JOB_WATCH_POLL_S")
    job_watch_grace_s: int = Field(180, alias="JOB_WATCH_GRACE_S")
    job_log_tail_lines: int = Field(2000, alias="JOB_LOG_TAIL_LINES")

    # Per-window job quotas (24h sliding window).
    job_per_agent_per_day: int = Field(10, alias="JOB_PER_AGENT_PER_DAY")
    job_per_user_per_day: int = Field(30, alias="JOB_PER_USER_PER_DAY")

    # ── Automated verification on new SOTA (optional; requires jobs) ──
    # The private eval set lives in the audit bucket (never the org-readable
    # central bucket) under {private_dataset_prefix}/ and is mounted read-only
    # at /private; rw job state also lands in the audit bucket because private
    # data may echo into the job output.
    verifier_enabled: bool = Field(False, alias="VERIFIER_ENABLED")
    verifier_agent: str = Field("", alias="VERIFIER_AGENT")
    private_dataset_prefix: str = Field("eval_dataset", alias="PRIVATE_DATASET_PREFIX")
    verification_runs_prefix: str = Field(
        "verification_runs", alias="VERIFICATION_RUNS_PREFIX"
    )
    # Relative tolerance on the re-run score: |rerun - reported| / reported.
    score_tol: float = Field(0.05, alias="VERIFIER_SCORE_TOL")
    # Optional guardrail: a summary.json field that must stay <= guard_cap
    # (e.g. a quality metric like perplexity). Empty = no guardrail.
    guard_field: str = Field("", alias="VERIFIER_GUARD_FIELD")
    guard_cap: float = Field(0.0, alias="VERIFIER_GUARD_CAP")

    @model_validator(mode="after")
    def _derive_defaults(self) -> "Settings":
        if not self.central_bucket:
            self.central_bucket = f"{self.org}/{self.collab_slug}-main-bucket"
        return self

    @property
    def agent_bucket_prefix(self) -> str:
        return f"{self.collab_slug}-"

    @property
    def required_result_field_list(self) -> list[str]:
        fields = [f.strip() for f in self.required_result_fields.split(",") if f.strip()]
        if self.score_field not in fields:
            fields.insert(0, self.score_field)
        return fields

    @property
    def job_extra_arg_list(self) -> list[str]:
        try:
            args = json.loads(self.job_extra_args)
        except json.JSONDecodeError:
            raise ValueError(f"JOB_EXTRA_ARGS is not valid JSON: {self.job_extra_args!r}")
        if not isinstance(args, list):
            raise ValueError("JOB_EXTRA_ARGS must be a JSON list of strings")
        return [str(a) for a in args]

    def agent_bucket(self, agent_id: str) -> str:
        return f"{self.org}/{self.collab_slug}-{agent_id}"

    def better(self, a: float, b: float) -> bool:
        """True iff score ``a`` beats score ``b`` under the configured order."""
        return a > b if self.score_order == "desc" else a < b

    def resolved_token(self) -> str:
        if self.hf_token:
            return self.hf_token
        cached = get_token()
        if not cached:
            raise RuntimeError(
                "no HF token available; set HF_TOKEN or run `hf auth login`"
            )
        return cached


@lru_cache
def get_settings() -> Settings:
    return Settings()
