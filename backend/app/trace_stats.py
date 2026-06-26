"""Trace manifests and the project token aggregate (see TRACES_DESIGN.md).

Traces live nested — ``traces/{agent}/{session}/manifest.md`` — so the shared
filename grammar in ``listing.py`` (which keys on a stamped ``*_{agent}.md``
leaf) does not apply: every manifest is named ``manifest.md`` and identity comes
from the path. This module holds the trace-specific validation, the
``stats``/``full`` completeness check, the listing pipeline, and the aggregate.

The cardinal rule mirrors the schema: an absent/``null`` metric means UNKNOWN and
is excluded; a ``0`` means genuinely zero. Never treat a missing number as 0.
"""
from __future__ import annotations

from datetime import date, datetime

from app.errors import InvalidFrontmatter
from app.models import (
    DigestStats,
    StatsResponse,
    TokenTotals,
    TraceSummary,
)
from app.naming import split_trace_manifest_path
from app.read_model import Record


# Harnesses whose adapters are expected to deliver the full enforced set
# (tokens + tool_calls). Anything else degrades to `partial` (never blocked).
KNOWN_FULL_HARNESSES: frozenset[str] = frozenset({"claude-code", "codex"})

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = ("schema_version", "harness", "session_id")

# usage.<field> -> TokenTotals attribute. The aggregate sums these where present.
_USAGE_FIELDS: tuple[tuple[str, str], ...] = (
    ("total_tokens", "total"),
    ("input_tokens", "input"),
    ("output_tokens", "output"),
    ("cache_read_tokens", "cache_read"),
    ("cache_creation_tokens", "cache_creation"),
    ("reasoning_tokens", "reasoning"),
)


def _num(v: object) -> int | float | None:
    """A real number, or None for absent/null/bool/non-number."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _int(v: object) -> int | None:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _mapping(v: object) -> dict:
    return v if isinstance(v, dict) else {}


def _timestamp_ok(v: object) -> bool:
    if isinstance(v, datetime):
        return True
    # PyYAML may parse date-like scalars into date objects. Accept them as
    # parseable but prefer full timestamps from the client.
    if isinstance(v, date):
        return True
    if not isinstance(v, str) or not v.strip():
        return False
    s = v.strip()
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    try:
        datetime.strptime(s, "%Y-%m-%d %H:%M UTC")
        return True
    except ValueError:
        return False


# ───────────────────────── validation ─────────────────────────

def validate_trace_manifest(fm: dict) -> None:
    """Lenient: require only identity/provenance; type-check whatever stats are
    present. Missing stats are fine (graceful degradation for unknown harnesses);
    nonsensical stats (negative, wrong type) are rejected."""
    for field in REQUIRED_MANIFEST_FIELDS:
        if field not in fm or fm[field] in (None, ""):
            raise InvalidFrontmatter(f"trace manifest missing required field: {field}")

    usage = fm.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise InvalidFrontmatter("`usage` must be a mapping")
        for key, _attr in _USAGE_FIELDS:
            v = usage.get(key)
            if v is not None and (_int(v) is None or v < 0):
                raise InvalidFrontmatter(
                    f"`usage.{key}` must be a non-negative integer or null"
                )

    cost = fm.get("cost_usd")
    if cost is not None and (_num(cost) is None or cost < 0):
        raise InvalidFrontmatter("`cost_usd` must be a non-negative number or null")

    activity = fm.get("activity")
    if activity is not None:
        if not isinstance(activity, dict):
            raise InvalidFrontmatter("`activity` must be a mapping")
        tc = activity.get("tool_calls")
        if tc is not None and (_int(tc) is None or tc < 0):
            raise InvalidFrontmatter(
                "`activity.tool_calls` must be a non-negative integer or null"
            )

    for key in ("started_at", "ended_at"):
        if fm.get(key) is not None and not _timestamp_ok(fm[key]):
            raise InvalidFrontmatter(f"`{key}` must be a parseable timestamp or null")


def completeness(fm: dict) -> str:
    """`full` iff a known harness delivered the enforced set (a numeric
    total_tokens AND a numeric tool_calls); else `partial`. Recorded on the
    manifest so the library/aggregate can surface adapter drift."""
    usage = _mapping(fm.get("usage"))
    activity = _mapping(fm.get("activity"))
    has_tokens = _int(usage.get("total_tokens")) is not None
    has_tools = _int(activity.get("tool_calls")) is not None
    if str(fm.get("harness", "")) in KNOWN_FULL_HARNESSES and has_tokens and has_tools:
        return "full"
    return "partial"


# ───────────────────────── summaries & listing ─────────────────────────

def _excerpt(body: str, n: int = 280) -> str:
    text = body.strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def trace_summary(
    rec: Record,
    agent: str,
    session: str,
    *,
    primary_log_file: str | None = None,
) -> TraceSummary:
    fm = rec.frontmatter
    usage = _mapping(fm.get("usage"))
    activity = _mapping(fm.get("activity"))
    total = _int(usage.get("total_tokens"))
    return TraceSummary(
        agent=agent,
        session_id=session,
        harness=_str_or_none(fm.get("harness")),
        model=_str_or_none(fm.get("model")),
        share=_str_or_none(fm.get("share")),
        completeness=_str_or_none(fm.get("completeness")),
        promoted_at=_str_or_none(fm.get("promoted_at")),
        started_at=_str_or_none(fm.get("started_at")),
        total_tokens=int(total) if total is not None else None,
        tool_calls=_int(activity.get("tool_calls")),
        result_ref=_str_or_none(fm.get("result_ref")),
        summary_excerpt=_excerpt(rec.body),
        path=f"traces/{agent}/{session}/",
        primary_log_file=primary_log_file,
    )


def _str_or_none(v: object) -> str | None:
    return str(v) if v not in (None, "") else None


def _q_match(rec: Record, q: str) -> bool:
    ql = q.lower()
    if ql in rec.body.lower():
        return True
    return any(ql in f"{k}: {v}".lower() for k, v in rec.frontmatter.items())


def _cursor_key(fm: dict, agent: str, session: str) -> str:
    """Recency cursor: server-stamped `promoted_at` (always present, sortable
    `YYYY-MM-DD HH:MM UTC`) plus the id as a stable tiebreaker."""
    return f"{fm.get('promoted_at', '')}|{agent}/{session}"


def list_traces(
    records: list[Record],
    *,
    agent: str | None,
    harness: str | None,
    model: str | None,
    share: str | None,
    q: str | None,
    expand: bool,
    limit: int | None,
    order: str,
    after: str | None,
    before: str | None,
    expand_cap: int,
    primary_log_files: dict[tuple[str, str], str] | None = None,
) -> tuple[int, int, list[str] | list[TraceSummary], str | None]:
    """Returns (count, matched, items, next). `count` = total manifests; items
    are `<agent>/<session>` ids unless `expand`. `next` is the opaque cursor."""
    rows: list[tuple[str, TraceSummary]] = []
    total = 0
    for rec in records:
        ids = split_trace_manifest_path(rec.path)
        if ids is None:
            continue
        total += 1
        a, s = ids
        fm = rec.frontmatter
        if agent is not None and a != agent:
            continue
        if harness is not None and str(fm.get("harness", "")) != harness:
            continue
        if model is not None and str(fm.get("model", "")) != model:
            continue
        if share is not None and str(fm.get("share", "")) != share:
            continue
        if q is not None and not _q_match(rec, q):
            continue
        rows.append(
            (
                _cursor_key(fm, a, s),
                trace_summary(
                    rec,
                    a,
                    s,
                    primary_log_file=(primary_log_files or {}).get((a, s)),
                ),
            )
        )

    rows.sort(key=lambda x: x[0])
    matched = len(rows)
    if after is not None:
        rows = [x for x in rows if x[0] > after]
    if before is not None:
        rows = [x for x in rows if x[0] < before]
    ordered = list(reversed(rows)) if order == "desc" else rows

    eff = expand_cap if (expand and (limit is None or limit <= 0 or limit > expand_cap)) else limit
    nxt: str | None = None
    if eff is not None and 0 < eff < len(ordered):
        ordered = ordered[:eff]
        nxt = ordered[-1][0]

    items: list[str] | list[TraceSummary]
    if expand:
        items = [s for _k, s in ordered]
    else:
        items = [f"{s.agent}/{s.session_id}" for _k, s in ordered]
    return total, matched, items, nxt


# ───────────────────────── aggregate ─────────────────────────

def _accumulate(t: TokenTotals, usage: dict) -> None:
    for key, attr in _USAGE_FIELDS:
        v = _int(usage.get(key))
        if v is not None:
            setattr(t, attr, getattr(t, attr) + v)


def _day_of(*candidates: object) -> str:
    """Date partition from the first ISO/`stamp_yaml` timestamp that looks like
    YYYY-MM-DD..."""
    for c in candidates:
        if not c:
            continue
        s = str(c)
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            return s[:10]
    return "unknown"


def aggregate(records: list[Record], *, generated_at: str) -> StatsResponse:
    totals = TokenTotals()
    by_model: dict[str, TokenTotals] = {}
    by_agent: dict[str, TokenTotals] = {}
    by_day: dict[str, TokenTotals] = {}
    agents: set[str] = set()
    counted = 0
    missing = 0
    cost_sum = 0.0
    cost_seen = False

    for rec in records:
        ids = split_trace_manifest_path(rec.path)
        if ids is None:
            continue
        agent, _session = ids
        agents.add(agent)
        fm = rec.frontmatter
        usage = _mapping(fm.get("usage"))
        if _int(usage.get("total_tokens")) is None:
            missing += 1
            continue
        counted += 1
        _accumulate(totals, usage)
        _accumulate(by_model.setdefault(str(fm.get("model") or "unknown"), TokenTotals()), usage)
        _accumulate(by_agent.setdefault(agent, TokenTotals()), usage)
        _accumulate(by_day.setdefault(_day_of(fm.get("started_at"), fm.get("promoted_at")), TokenTotals()), usage)
        c = _num(fm.get("cost_usd"))
        if c is not None:
            cost_sum += float(c)
            cost_seen = True

    return StatsResponse(
        tokens=totals,
        cost_usd=round(cost_sum, 6) if cost_seen else None,
        sessions_counted=counted,
        sessions_missing_tokens=missing,
        agents_reporting=len(agents),
        by_model=by_model,
        by_agent=by_agent,
        by_day=by_day,
        generated_at=generated_at,
    )


def digest_stats(stats: StatsResponse) -> DigestStats:
    return DigestStats(
        total_tokens=stats.tokens.total,
        sessions_counted=stats.sessions_counted,
        agents_reporting=stats.agents_reporting,
    )
