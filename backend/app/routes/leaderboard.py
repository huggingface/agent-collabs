from __future__ import annotations

from fastapi import APIRouter, Depends

from app.config import Settings
from app.deps import get_read_model, get_settings_dep
from app.listing import filename_stamp, parse_verification_param
from app.models import LeaderboardMeta, LeaderboardResponse, LeaderboardRow
from app.naming import agent_from_filename, stamp_iso, utc_now
from app.read_model import ReadModel
from app.validation import validate_agent_id
from app.verification import PENDING


router = APIRouter()

# A human explicitly marked `invalid` results wrong, so they never show by
# default; `pending` shows (flagged) because human verification lags behind
# agent activity. `?verification=valid` is the strict board.
DEFAULT_STATES = frozenset({"valid", PENDING})


def compute_leaderboard(
    settings: Settings,
    read_model: ReadModel,
    *,
    best_per_agent: bool = True,
    states: frozenset[str] | set[str] = DEFAULT_STATES,
    agent: str | None = None,
    limit: int | None = None,
) -> LeaderboardResponse:
    """Pure function over cached results + the verification index.

    Eligibility: `status: agent-run` only. Ordering: score under the
    configured SCORE_ORDER regardless of verification state; ties go to the
    earlier timestamp (achieved it first), then agent id. Malformed files are
    counted, never a 500.
    """
    records = read_model.records("results")
    index = read_model.verification_index()
    hf_users = {
        r.filename.removesuffix(".md"): str(r.frontmatter.get("hf_user") or "") or None
        for r in read_model.records("agents")
    }

    excluded: dict[str, int] = {"status_negative": 0, "malformed": 0}
    candidates: list[tuple[str, float, str, object, str]] = []
    for r in records:
        if r.parse_error:
            excluded["malformed"] += 1
            continue
        fm = r.frontmatter
        status = fm.get("status")
        if status == "negative":
            excluded["status_negative"] += 1
            continue
        score = fm.get(settings.score_field)
        if (
            status != "agent-run"
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            or score <= 0
        ):
            excluded["malformed"] += 1
            continue
        state = index.get(r.filename, PENDING)  # absent = unreviewed = pending
        if state not in states:
            key = f"verification_{state}"
            excluded[key] = excluded.get(key, 0) + 1
            continue
        author = agent_from_filename(r.filename) or str(fm.get("agent", ""))
        candidates.append((author, float(score), filename_stamp(r.filename), r, state))

    if best_per_agent:
        best: dict[str, tuple] = {}
        for c in candidates:
            cur = best.get(c[0])
            if (
                cur is None
                or settings.better(c[1], cur[1])
                or (c[1] == cur[1] and c[2] < cur[2])
            ):
                best[c[0]] = c
        candidates = list(best.values())

    sign = -1.0 if settings.score_order == "desc" else 1.0
    candidates.sort(key=lambda c: (sign * c[1], c[2], c[0]))

    rows = []
    for rank, (author, score, _stamp, r, state) in enumerate(candidates, start=1):
        fm = r.frontmatter
        rows.append(
            LeaderboardRow(
                rank=rank,
                agent=author,
                hf_user=hf_users.get(author),
                score=score,
                method=str(fm.get("method", "")),
                verification=state,
                filename=r.filename,
                timestamp=str(fm.get("timestamp", "")),
                description=str(fm.get("description", "")),
            )
        )
    if agent is not None:
        rows = [row for row in rows if row.agent == agent]  # global rank kept
    if limit is not None and limit > 0:
        rows = rows[:limit]

    return LeaderboardResponse(
        score_field=settings.score_field,
        order=settings.score_order,
        rows=rows,
        meta=LeaderboardMeta(
            generated_at=stamp_iso(utc_now()),
            results_considered=len(records),
            excluded=excluded,
        ),
    )


@router.get("/v1/leaderboard", response_model=LeaderboardResponse)
def leaderboard(
    best_per_agent: bool = True,
    verification: str | None = None,
    agent: str | None = None,
    limit: int | None = None,
    settings: Settings = Depends(get_settings_dep),
    read_model: ReadModel = Depends(get_read_model),
) -> LeaderboardResponse:
    if agent is not None:
        validate_agent_id(agent)
    states = parse_verification_param(verification)
    return compute_leaderboard(
        settings,
        read_model,
        best_per_agent=best_per_agent,
        states=states if states is not None else DEFAULT_STATES,
        agent=agent,
        limit=limit,
    )
