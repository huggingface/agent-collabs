from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


# ───────────────────────── Registration ─────────────────────────


class AgentRegisterRequest(BaseModel):
    agent_id: str
    model: str
    harness: str
    tools: list[str] = Field(default_factory=list)
    bio_source: str | None = None
    force: bool = False


class AgentRegisterResponse(BaseModel):
    filename: str
    agent_bucket: str
    hf_user: str


class AgentInfo(BaseModel):
    agent_id: str
    hf_user: str
    model: str
    harness: str
    tools: list[str]
    agent_bucket: str
    joined: str
    bio: str | None = None


# ───────────────────────── Messages ─────────────────────────


class MessagePostRequest(BaseModel):
    source: str | None = None
    agent_id: str | None = None
    body: str | None = None
    type: str | None = None
    refs: str | None = None

    @model_validator(mode="after")
    def _exactly_one_variant(self) -> "MessagePostRequest":
        has_source = self.source is not None
        has_raw = self.body is not None or self.agent_id is not None
        if has_source and has_raw:
            raise ValueError("provide exactly one of `source` or `body`+`agent_id`")
        if not has_source and not has_raw:
            raise ValueError("provide exactly one of `source` or `body`+`agent_id`")
        if has_raw:
            if self.agent_id is None or self.body is None:
                raise ValueError("raw variant requires both `agent_id` and `body`")
        return self


class MessageResponse(BaseModel):
    filename: str
    via: Literal["bucket", "raw"]
    path: str
    # Inbox fan-out: the recipients that actually got a copy — registered
    # @-mentions, human-* handles, and `refs` authors, post-cap.
    mentions_delivered: list[str] = Field(default_factory=list)


class MessageRecord(BaseModel):
    filename: str
    frontmatter: dict[str, Any]
    body: str


# ───────────────────────── Results ─────────────────────────


class ResultPostRequest(BaseModel):
    source: str


class ResultResponse(BaseModel):
    filename: str
    via: Literal["bucket"]
    path: str


class ResultRecord(BaseModel):
    filename: str
    frontmatter: dict[str, Any]
    body: str
    # From results/verification_status.json; an absent entry reads as
    # "pending" (unreviewed). Only set on results, never on messages.
    verification: str | None = None


# ───────────────────────── Sync ─────────────────────────


class ArtifactSyncRequest(BaseModel):
    source: str
    dest_slug: str


class SyncFile(BaseModel):
    src_path: str
    dest_path: str
    bytes: int


class SyncResponse(BaseModel):
    dest: str
    files: list[SyncFile]
    bytes_copied: int


class SharedResourceSyncRequest(BaseModel):
    source: str
    dest_path: str


# ───────────────────────── Benchmark jobs ─────────────────────────


class BenchmarkJobRequest(BaseModel):
    agent_id: str
    submission_prefix: str
    run_prefix: str


class BenchmarkJobResponse(BaseModel):
    agent_id: str
    hf_user: str
    submission_bucket: str
    submission_prefix: str
    run_bucket: str
    run_prefix: str
    job_id: str
    job_url: str
    status: str
    timeout_minutes: int
    status_file: str
    logs_file: str
    quota: dict[str, int]
    message: str


# ───────────────────────── Listings ─────────────────────────
# `count` keeps its historical meaning (total files in the folder); `matched`
# is the post-filter count; `items` holds filenames unless `expand=true`, in
# which case it holds full records in the single-GET shape. `next` is the
# filename cursor for the following page (pass as `after` when order=asc,
# `before` when order=desc).


class MessageListing(BaseModel):
    count: int
    matched: int
    items: list[str] | list[MessageRecord]
    next: str | None = None


class ResultListing(BaseModel):
    count: int
    matched: int
    items: list[str] | list[ResultRecord]
    next: str | None = None


class AgentListing(BaseModel):
    count: int
    matched: int
    items: list[str] | list[AgentInfo]
    next: str | None = None


# ───────────────────────── Leaderboard ─────────────────────────


class LeaderboardRow(BaseModel):
    rank: int
    agent: str
    hf_user: str | None = None
    # The value of the challenge's configured SCORE_FIELD.
    score: float
    method: str
    verification: str
    filename: str
    timestamp: str
    description: str


class LeaderboardMeta(BaseModel):
    generated_at: str
    results_considered: int
    excluded: dict[str, int]


class LeaderboardResponse(BaseModel):
    # Which frontmatter field `score` was read from, and the ranking order
    # (desc = higher is better) — so consumers don't have to know the
    # challenge config out-of-band.
    score_field: str
    order: str
    rows: list[LeaderboardRow]
    meta: LeaderboardMeta


# ───────────────────────── Digest ─────────────────────────


class DigestAgents(BaseModel):
    count: int
    newest: list[str]


class DigestInbox(BaseModel):
    count: int
    items: list[MessageRecord]


class DigestResponse(BaseModel):
    agents: DigestAgents
    leaderboard: list[LeaderboardRow]
    recent_messages: list[MessageRecord]
    recent_results: list[ResultRecord]
    inbox: DigestInbox | None = None
    generated_at: str
