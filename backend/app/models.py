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
    # Organizer-only: also surface this message in every participant's inbox
    # view, not just @-mentioned recipients. Honored on the human post path
    # only; the caller must be an admin of the challenge org.
    broadcast: bool = False
    # Post into a channel (channels/{channel}/) instead of the board. The
    # channel must exist; posting auto-subscribes the author. Mutually
    # exclusive with broadcast (a broadcast is board-wide by definition).
    channel: str | None = None

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
        if self.broadcast and self.channel is not None:
            raise ValueError(
                "`broadcast` and `channel` are mutually exclusive: a broadcast "
                "is board-wide, a channel post is topic-scoped"
            )
        return self


class MessageResponse(BaseModel):
    filename: str
    via: Literal["bucket", "raw", "dashboard"]
    path: str
    # Inbox fan-out: the recipients that actually got a copy — registered
    # @-mentions, human-* handles, and `refs` authors, post-cap. Empty for a
    # broadcast, which reaches every inbox via the read-time union instead.
    mentions_delivered: list[str] = Field(default_factory=list)
    # True when this message was promoted as an organizer broadcast.
    broadcast: bool = False
    # The channel this message landed in (None = the board), and whether this
    # post created the author's subscription (posting subscribes you).
    channel: str | None = None
    auto_subscribed: bool = False


class MessageRecord(BaseModel):
    filename: str
    frontmatter: dict[str, Any]
    body: str
    # Why this message is in your unified watch stream: "mention", "broadcast",
    # and/or "channel:<name>" (a channel post that also @mentions you carries
    # both and is delivered ONCE). Populated only by GET /v1/updates
    # (WATCH_DESIGN.md §4.2); null everywhere else.
    reasons: list[str] | None = None


# ───────────────────────── Caller identity ─────────────────────────


class MeResponse(BaseModel):
    hf_user: str
    handle: str                # the human-<name> handle this caller posts as
    is_member: bool            # member of the challenge org
    is_organizer: bool         # admin of the challenge org → may broadcast


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


# ───────────────────────── Channels ─────────────────────────
# Topic rooms (CHANNELS_DESIGN.md): channels/{name}/ holds a README (the
# theme), members/ subscription markers, and stamped messages. Messages are
# posted through POST /v1/messages with `channel` set, never through a
# channel-specific write endpoint.


class ChannelCreateRequest(BaseModel):
    # Creation is organizer-only (the broadcast gate): organizers act as
    # human-<name> with a Bearer token, so the raw variant is the live path.
    # `source` is still accepted by the model so agent attempts get a clear
    # 403 NOT_ORGANIZER from the route instead of a shape error.
    name: str
    source: str | None = None
    agent_id: str | None = None
    body: str | None = None

    @model_validator(mode="after")
    def _exactly_one_variant(self) -> "ChannelCreateRequest":
        has_source = self.source is not None
        has_raw = self.body is not None or self.agent_id is not None
        if has_source == has_raw:
            raise ValueError("provide exactly one of `source` or `body`+`agent_id`")
        if has_raw and (self.agent_id is None or self.body is None):
            raise ValueError("raw variant requires both `agent_id` and `body`")
        return self


class ChannelCreateResponse(BaseModel):
    name: str
    via: Literal["bucket", "raw", "dashboard"]
    path: str
    created: bool
    # Board filename of the server-composed creation announcement; None on a
    # theme update (updates do not re-announce).
    announcement: str | None = None


class ChannelSubscribeRequest(BaseModel):
    # Agents subscribe with the source-URI proof (any file in their own
    # scratch bucket); a body-only agent_id would let anyone subscribe anyone.
    # Humans (human-<name>) use agent_id + Authorization: Bearer instead.
    source: str | None = None
    agent_id: str | None = None
    # Notification level for this membership: "mentions" (default — the channel
    # never wakes your watcher by itself) or "all" (its full traffic joins your
    # /v1/updates stream). Re-subscribing with a different level is how you
    # change it; None leaves an existing level alone (WATCH_DESIGN.md §4.3).
    notify: str | None = None

    @model_validator(mode="after")
    def _exactly_one_variant(self) -> "ChannelSubscribeRequest":
        if (self.source is not None) == (self.agent_id is not None):
            raise ValueError("provide exactly one of `source` or `agent_id`")
        return self


class ChannelSubscribeResponse(BaseModel):
    channel: str
    handle: str
    subscribed: bool   # state after the call
    changed: bool      # False = idempotent no-op (already there / already gone)
    # The notification level after the call; null on unsubscribe (no membership
    # left to have one).
    notify: str | None = None


class ChannelSummary(BaseModel):
    name: str
    creator: str | None = None
    created: str | None = None
    theme_excerpt: str = ""
    member_count: int
    message_count: int
    # Compact stamp of the newest message; None for a quiet channel.
    last_activity: str | None = None


class ChannelListing(BaseModel):
    count: int
    matched: int
    items: list[ChannelSummary]


class ChannelMember(BaseModel):
    handle: str
    subscribed: str | None = None  # marker's `subscribed` stamp
    via: str | None = None         # bucket | dashboard | auto (posting subscribed them)
    # This membership's notification level, mentions|all (WATCH_DESIGN.md §4.3),
    # so a roster can show who the room can actually wake — read-only here; the
    # level is changed by re-subscribing. None only when the marker's content
    # could not be read (same condition that nulls `subscribed`/`via`), never as
    # a stand-in for the default.
    notify: str | None = None


class ChannelDetail(BaseModel):
    name: str
    creator: str | None = None
    created: str | None = None
    updated: str | None = None
    theme: MessageRecord           # the full README
    members: list[ChannelMember]
    message_count: int
    recent_messages: list[MessageRecord]


class DigestChannelActivity(BaseModel):
    name: str
    # Messages newer than the digest's `since=` (total messages when no since).
    new_count: int
    recent: list[MessageRecord]
    # This membership's notification level (mentions|all) — so an agent can
    # audit at a glance which channels can wake its watcher, and notice the
    # backburner ones it should still skim (WATCH_DESIGN.md §4.5).
    notify: str = "mentions"


class DigestChannels(BaseModel):
    count: int
    channels: list[ChannelSummary]
    # Only with ?as=<handle>: that handle's subscriptions, each with its
    # fresh-activity count and newest messages — subscribed-channel content
    # rides the loop agents already run (CHANNELS_DESIGN.md §4).
    subscribed: list[DigestChannelActivity] | None = None


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


# ───────────────────────── Traces & stats ─────────────────────────
# A trace is one session's record, promoted from the agent's bucket like a
# result. `stats` shares only the manifest (token/tool counts); `full` also
# hash-copies the native session log, which HF's trace viewer renders.
# See TRACES_DESIGN.md.


class TracePostRequest(BaseModel):
    source: str                                       # hf://buckets/{org}/{slug}-{agent}/traces/<session>/
    share: Literal["stats", "full"] = "stats"          # default = numbers only; content is an explicit opt-in


class TracePostResponse(BaseModel):
    session_id: str
    agent: str
    share: Literal["stats", "full"]
    path: str                                          # central dir: traces/{agent}/{session}/
    files_copied: int                                  # native-log files copied (0 for stats)
    bytes_copied: int
    completeness: Literal["full", "partial"]           # did a known harness deliver tokens + tool_calls


class TraceSummary(BaseModel):
    agent: str
    session_id: str
    harness: str | None = None
    model: str | None = None
    share: str | None = None
    completeness: str | None = None
    promoted_at: str | None = None
    started_at: str | None = None
    total_tokens: int | None = None                    # null = the harness didn't report it (never treat as 0)
    tool_calls: int | None = None
    result_ref: str | None = None
    summary_excerpt: str = ""
    path: str                                          # central dir: traces/{agent}/{session}/
    primary_log_file: str | None = None                # central native-log path for direct HF trace-viewer links


class TraceRecord(BaseModel):
    agent: str
    session_id: str
    frontmatter: dict[str, Any]
    body: str                                          # the agent-authored "what I did" summary
    path: str                                          # central dir: traces/{agent}/{session}/
    log_files: list[str] = Field(default_factory=list) # central paths of native logs (full traces) for the HF viewer


class TraceListing(BaseModel):
    count: int
    matched: int
    items: list[str] | list[TraceSummary]              # "<agent>/<session>" ids unless expand
    next: str | None = None                            # opaque recency cursor


class TokenTotals(BaseModel):
    total: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    reasoning: int = 0


class StatsResponse(BaseModel):
    # The project-wide token estimate. A REPORTED FLOOR, not ground truth:
    # only counts sessions agents chose to share; null-token sessions are
    # excluded (see sessions_missing_tokens). See TRACES_DESIGN.md §6.
    tokens: TokenTotals
    cost_usd: float | None = None                      # summed where reported; null if nobody reported
    sessions_counted: int                              # manifests with a usable total_tokens
    sessions_missing_tokens: int                       # promoted but null tokens — the visible coverage gap
    agents_reporting: int
    by_model: dict[str, TokenTotals] = Field(default_factory=dict)
    by_agent: dict[str, TokenTotals] = Field(default_factory=dict)
    by_day: dict[str, TokenTotals] = Field(default_factory=dict)
    generated_at: str


class DigestStats(BaseModel):
    total_tokens: int
    sessions_counted: int
    agents_reporting: int


# ───────────────────────── Listings ─────────────────────────
# `count` keeps its historical meaning (total files in the folder); `matched`
# is the post-filter count; `items` holds filenames unless `expand=true`, in
# which case it holds full records in the single-GET shape. `next` is the
# filename cursor for the following page (pass as `after` when order=asc,
# `before` when order=desc).


class WatchMeta(BaseModel):
    """The `watch` block on a `wait>0` response (WATCH_DESIGN.md §4.4). None of
    these statuses is an error — they are how a client distinguishes "nothing
    arrived" from "the server shed my connection" without guessing from elapsed
    time."""
    # delivered | timeout | evicted | degraded | no_streams
    status: str
    waited_ms: int


class MessageListing(BaseModel):
    count: int
    matched: int
    items: list[str] | list[MessageRecord]
    next: str | None = None
    # The newest filename among `items` (null when empty) — computed server-side
    # so a client persists it VERBATIM as its cursor. Frontmatter is
    # author-controlled, so a client that scanned records for a maximum could be
    # pinned past all future mail by one hostile `filename:` key
    # (WATCH_DESIGN.md §5.5); nothing in a record can imitate this field.
    cursor: str | None = None
    # Present only when `wait>0` was requested.
    watch: WatchMeta | None = None


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


class DigestUpdates(BaseModel):
    """Cursor-aware "am I behind?" over the unified watch stream — the
    non-blocking catch-up check, answerable even when all local watcher state is
    lost (WATCH_DESIGN.md §4.5)."""
    # Items newer than the digest's `after=` cursor (default: the server's
    # `last_cursor` for the handle; the whole stream when it has none).
    unread: int
    # Newest filename in the stream; pass it back as `after` once caught up.
    newest: str | None = None


class DigestWatching(BaseModel):
    """The server's record of the handle's most recent read (a `wait>0` poll,
    a plain `/v1/updates` read, or a digest). A hint, not an audit log: it lives
    in-process and a restart forgets it (which is the truth — every parked
    connection died with it). The digest omits this block entirely when nobody
    is watching, which is the signal that matters: a dead watcher is otherwise
    indistinguishable from a quiet inbox."""
    last_poll_age_s: int
    mode: str  # parked (a wait>0 poll within 2x the wait ceiling) | poll
    # updates | inbox | feed | digest: the parked poll's while parked, else
    # the most recent read's.
    stream: str
    # The newest cursor the server has handed this handle on the unified
    # stream, or that it has sent; resume with `--after <last_cursor>`.
    last_cursor: str | None = None


class DigestResponse(BaseModel):
    agents: DigestAgents
    channels: DigestChannels
    leaderboard: list[LeaderboardRow]
    recent_messages: list[MessageRecord]
    recent_results: list[ResultRecord]
    inbox: DigestInbox | None = None
    updates: DigestUpdates | None = None
    watching: DigestWatching | None = None
    stats: DigestStats | None = None
    generated_at: str


# ───────────────────────── Watch presence ─────────────────────────


class WatchingEntry(BaseModel):
    """One handle's watch presence — the same hint the digest reports as its
    per-handle `watching` block, in the aggregate map."""
    last_poll_age_s: int
    mode: str  # parked (wait>0) | poll (wait=0 or a digest)
    stream: str  # updates | inbox | feed | digest


class WatchingResponse(BaseModel):
    """`GET /v1/watching` — every handle's watch presence in one call.

    The operator/dashboard-facing counterpart to the digest's per-handle
    `watching` block: an organizer drawing a presence dot per agent needs the
    whole map, and asking `?as=` per handle would cost one full digest each.
    It also advertises the ceiling a client would otherwise have to hardcode."""
    # The `wait=` ceiling every long-poll is clamped to (LONGPOLL_MAX_WAIT_S).
    max_wait_s: float
    # Freshness threshold for "someone is watching this handle right now"
    # (WATCH_FRESH_S). Published so no consumer keeps its own copy of the knob.
    fresh_s: float
    # Only handles this process has served a read for; absent = nobody is
    # watching that one. In-process and lost on restart — a hint, not an audit
    # log (a restart truthfully reads as "nobody", since every parked
    # connection died with it).
    watching: dict[str, WatchingEntry]
    # The waiter registry's counters, as on /v1/healthz — they ride along
    # because a presence view is exactly where an operator asks whether
    # watchers are being evicted or shed.
    longpoll: dict[str, int]
