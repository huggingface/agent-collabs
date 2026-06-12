#!/usr/bin/env python3
"""Offline verification reconciler / trigger (§5.7).

Run by an admin whose HF token can write the central bucket, read the private
audit bucket, and launch org jobs (the same token the Space holds):

    HF_TOKEN=hf_... python scripts/verify_submissions.py reconcile [--dry-run]
    HF_TOKEN=hf_... python scripts/verify_submissions.py trigger [--dry-run] [--limit N]

``reconcile`` is the restart-safety net for the in-Space verifier: its watcher
threads are in-memory (DESIGN §1), so a Space restart mid-verification loses
the verdict even though the job finishes and writes ``summary.json``. This
mode scans ``verification_runs/*`` in the audit bucket for completed runs
whose index entry is still ``pending``/absent, then computes the verdict,
writes it through the SAME compare-and-set (human verdicts win), and posts the
same announcement — all via ``app.verifier``, one canonical implementation, so
online and offline behavior cannot drift. Idempotent: a run already recorded
(or human-decided) is skipped, so it is safe to re-run or schedule.

``trigger`` is the backfill for results that were posted before the trigger
existed (or whose launch failed): it walks ``agent-run`` results from the
best claimed score down and fires the same ``maybe_trigger`` the POST hook
uses. Launches run SYNCHRONOUSLY (the script polls the job to terminal state,
~15–40 min each), and each recorded ``valid`` raises the champion bar for the
candidates after it — champion-search semantics. This mode SPENDS org credits;
use ``--dry-run`` first.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.hub import HubClient  # noqa: E402
from app.jobs import JobRunner  # noqa: E402
from app.naming import agent_from_filename  # noqa: E402
from app.read_model import ReadModel  # noqa: E402
from app.verification import PENDING, VerificationStatusStore  # noqa: E402
from app.verifier import Verifier, compute_verdict  # noqa: E402


def build_verifier(settings: Settings) -> tuple[Verifier, HubClient, ReadModel]:
    hub = HubClient(settings)
    read_model = ReadModel(hub, settings)
    verification = VerificationStatusStore(
        hub, runs_prefix=settings.verification_runs_prefix
    )
    runner = JobRunner(settings, hub)
    # Inline spawn: watchers run synchronously so the script doesn't exit
    # while a job is still being supervised.
    verifier = Verifier(
        settings, hub, read_model, verification, runner,
        spawn=lambda _name, fn: fn(),
    )
    return verifier, hub, read_model


def _run_dirs(hub: HubClient, settings: Settings) -> dict[str, set[str]]:
    """{result filename: {leaf files in its verification run dir}}."""
    prefix = settings.verification_runs_prefix
    out: dict[str, set[str]] = {}
    for e in hub.list_bucket_dir(settings.audit_bucket, prefix):
        rel = e.rel_path[len(prefix) + 1 :]
        if "/" not in rel:
            continue
        filename, leaf = rel.split("/", 1)
        out.setdefault(filename, set()).add(leaf)
    return out


def _reported_score(
    hub: HubClient,
    read_model: ReadModel,
    settings: Settings,
    filename: str,
) -> float | None:
    """The score the result claimed: from verification_request.json (written at
    launch), falling back to the promoted result's frontmatter."""
    raw = hub.read_audit_bytes(
        f"{settings.verification_runs_prefix}/{filename}/verification_request.json"
    )
    if raw is not None:
        try:
            score = json.loads(raw.decode("utf-8")).get("reported_score")
            if isinstance(score, (int, float)) and not isinstance(score, bool) and score > 0:
                return float(score)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    rec = read_model.record("results", filename)
    if rec is not None:
        score = rec.frontmatter.get(settings.score_field)
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score > 0:
            return float(score)
    return None


def reconcile(settings: Settings, dry_run: bool) -> int:
    verifier, hub, read_model = build_verifier(settings)
    index = read_model.verification_index()
    runs = _run_dirs(hub, settings)
    print(f"run_dirs={len(runs)} indexed={len(index)}")

    healed = skipped = undecided = 0
    for filename, leaves in sorted(runs.items()):
        state = index.get(filename, PENDING)
        if state != PENDING:
            skipped += 1
            continue
        if "summary.json" not in leaves:
            print(f"  {filename}: no summary.json yet (job running/failed); skipping")
            skipped += 1
            continue
        raw = hub.read_audit_bytes(
            f"{settings.verification_runs_prefix}/{filename}/summary.json"
        )
        if raw is None:
            skipped += 1
            continue
        try:
            summary = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"  {filename}: unparseable summary.json ({exc}); skipping")
            skipped += 1
            continue
        reported_score = _reported_score(hub, read_model, settings, filename)
        if reported_score is None:
            print(f"  {filename}: no reported score on record; skipping")
            skipped += 1
            continue
        owner = agent_from_filename(filename) or ""
        if dry_run:
            verdict, details = compute_verdict(settings, reported_score, summary)
            print(f"  {filename}: would record {verdict} ({details})")
            healed += verdict is not None
            undecided += verdict is None
            continue
        verdict = verifier.record_verdict(
            filename,
            owner=owner,
            reported_score=reported_score,
            summary=summary,
            job_id=summary.get("job_id"),
        )
        print(f"  {filename}: {verdict or 'left pending / deferred'}")
        healed += verdict is not None
        undecided += verdict is None
    print(f"healed={healed} undecided={undecided} skipped={skipped}")
    return 0


def _claimed_score(settings: Settings, record) -> float:
    score = record.frontmatter.get(settings.score_field)
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return 0.0
    return float(score)


def trigger(settings: Settings, dry_run: bool, limit: int | None) -> int:
    verifier, _hub, read_model = build_verifier(settings)
    records = read_model.records("results")
    candidates = sorted(
        (r for r in records if not r.parse_error),
        key=lambda r: _claimed_score(settings, r),
        reverse=settings.score_order == "desc",
    )
    launched = 0
    for r in candidates:
        if limit is not None and launched >= limit:
            break
        # Re-check per candidate: each recorded `valid` raises the bar (the
        # champion) for everything after it — champion search from the best
        # claim down.
        if not verifier.should_verify(r.filename, r.frontmatter):
            continue
        claimed = r.frontmatter.get(settings.score_field)
        if dry_run:
            print(f"  would verify {r.filename} ({settings.score_field}={claimed})")
            launched += 1
            continue
        print(f"  verifying {r.filename} ({settings.score_field}={claimed}) ...")
        if verifier.maybe_trigger(r.filename, r.frontmatter):
            launched += 1
    print(f"{'would launch' if dry_run else 'launched'}={launched}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)
    p_rec = sub.add_parser("reconcile", help="record verdicts for completed runs")
    p_rec.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    p_trg = sub.add_parser("trigger", help="launch verification for unverified SOTA claims")
    p_trg.add_argument("--dry-run", action="store_true", help="report only, launch nothing")
    p_trg.add_argument("--limit", type=int, default=None, help="max launches this run")
    args = ap.parse_args()

    # The offline tool is always 'enabled' — the env flag gates only the
    # in-Space trigger.
    settings = Settings(VERIFIER_ENABLED=True)
    if args.mode == "reconcile":
        return reconcile(settings, args.dry_run)
    return trigger(settings, args.dry_run, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
