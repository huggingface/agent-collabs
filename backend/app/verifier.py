"""Automated verification on new SOTA.

When ``POST /v1/results`` promotes an ``agent-run`` result whose claimed score
beats the current verified-``valid`` champion, this module re-runs the
submission behind it on the **private** eval set (an org-credit HF Job with
the audit bucket mounted), decides a verdict, records it through the CAS write
path (``VerificationStatusStore.set_verdict`` — human verdicts always win),
and announces the outcome on the message board as the verifier identity.

The trigger is in-process: ``maybe_trigger`` runs inside the POST (cheap reads
+ one job launch), then a watcher thread supervises the job — the same pattern
``POST /v1/jobs:run`` uses. All trigger/watcher state is in-memory: a Space
restart drops in-flight watchers, and the offline reconciler
(``scripts/verify_submissions.py reconcile``) heals completed-but-unrecorded
runs through these same functions, so online and offline behavior cannot
drift.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from app.announce import post_server_message
from app.config import Settings
from app.hub import HubClient
from app.jobs import JobRunner
from app.naming import agent_from_filename, parse_source_uri, stamp_iso, utc_now
from app.read_model import ReadModel, Record
from app.verification import INVALID, VALID, WRITTEN, VerificationStatusStore


log = logging.getLogger(__name__)


def _thread_spawn(name: str, fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name=name, daemon=True).start()


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


# ───────────────────────── pure verdict logic ─────────────────────────


def champion_score(
    settings: Settings, records: list[Record], index: dict[str, str]
) -> float | None:
    """The best score among verified-``valid`` ``agent-run`` results.

    None when there is no valid champion yet (cold start) — the first
    ``agent-run`` result then seeds the champion.
    """
    best: float | None = None
    for r in records:
        fm = r.frontmatter
        if fm.get("status") != "agent-run":
            continue
        score = _positive_number(fm.get(settings.score_field))
        if score is None:
            continue
        if index.get(r.filename) != VALID:
            continue
        if best is None or settings.better(score, best):
            best = score
    return best


def compute_verdict(
    settings: Settings,
    reported_score: float,
    summary: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    """Verdict over a private-set re-run summary.

    ``valid`` iff |rerun − reported| / reported ≤ score_tol AND (no guard
    configured, or rerun guard ≤ guard_cap). Returns (verdict, details);
    verdict is None when the summary lacks usable numbers — undecidable,
    leave ``pending``.
    """
    rerun_score = _positive_number(summary.get(settings.score_field))
    if rerun_score is None or reported_score <= 0:
        return None, {}
    delta_frac = abs(rerun_score - reported_score) / reported_score
    score_ok = delta_frac <= settings.score_tol
    details: dict[str, Any] = {
        "rerun_score": rerun_score,
        "score_delta_frac": round(delta_frac, 6),
        "score_ok": score_ok,
    }
    guard_ok = True
    if settings.guard_field:
        rerun_guard = _number(summary.get(settings.guard_field))
        if rerun_guard is None:
            return None, {}
        guard_ok = rerun_guard <= settings.guard_cap
        details["rerun_guard"] = rerun_guard
        details["guard_ok"] = guard_ok
    return (VALID if score_ok and guard_ok else INVALID), details


# ───────────────────────── announcement bodies ─────────────────────────


def verdict_body(
    settings: Settings,
    *,
    owner: str,
    filename: str,
    verdict: str,
    reported_score: float,
    details: dict[str, Any],
) -> str:
    delta_pct = details["score_delta_frac"] * 100
    label = settings.score_field
    rows = [
        (f"reported {label}", f"{reported_score:.2f}", "—"),
        (
            f"re-run {label} (private set)",
            f"{details['rerun_score']:.2f} (Δ {delta_pct:.1f}%)",
            f"Δ ≤ {settings.score_tol * 100:.0f}% " + ("✅" if details["score_ok"] else "❌"),
        ),
    ]
    if settings.guard_field and "rerun_guard" in details:
        rows.append(
            (
                f"re-run {settings.guard_field}",
                f"{details['rerun_guard']:.4f}",
                f"≤ {settings.guard_cap} " + ("✅" if details["guard_ok"] else "❌"),
            )
        )
    table = "| metric | value | check |\n| --- | --- | --- |\n" + "\n".join(
        f"| {m} | {v} | {c} |" for m, v, c in rows
    )
    if verdict == VALID:
        head = (
            f"🎉 @{owner} your result `{filename}` claimed a new SOTA and was "
            "re-run on the **private** eval set: **VERIFIED VALID**."
        )
    else:
        head = (
            f"@{owner} your result `{filename}` was re-run on the **private** "
            "eval set and came back **INVALID**."
        )
    return f"{head}\n\n{table}\n"


def unreproducible_body(*, owner: str, filename: str) -> str:
    return (
        f"@{owner} your result `{filename}` claims a new SOTA, but we couldn't "
        "reproduce it: no runnable submission could be located from its "
        "frontmatter. Point `artifacts:` (or `submission:`) at a directory "
        "containing your runnable submission — either directly, or at a run "
        "directory whose `run_request.json`/`job_status.json` names the "
        "submission. The result stays `pending` until it can be verified."
    )


# ───────────────────────── the verifier ─────────────────────────


class Verifier:
    """SOTA check, submission resolution, launch + verdict watcher.

    ``runner`` provides ``launch_verification`` / ``watch_terminal`` /
    ``fetch_logs_text`` (the real ``JobRunner`` or a test fake); ``spawn`` runs
    the watcher (a daemon thread by default, inline in tests/scripts).
    """

    def __init__(
        self,
        settings: Settings,
        hub: HubClient,
        read_model: ReadModel,
        verification: VerificationStatusStore,
        runner: JobRunner,
        *,
        spawn: Callable[[str, Callable[[], None]], None] = _thread_spawn,
    ):
        self._settings = settings
        self._hub = hub
        self._read_model = read_model
        self._verification = verification
        self._runner = runner
        self._spawn = spawn
        # Single-flight per result: stops the SAME result being launched twice
        # and two watchers racing one verdict. Not a spend cap — different
        # results verify in parallel.
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    # ───────────────── trigger ─────────────────

    def maybe_trigger(self, filename: str, fm: dict[str, Any]) -> bool:
        """Hook on result promotion. Best-effort: never raises — by the time
        we get here the result is already promoted, so a verifier failure must
        not fail the POST. Returns True iff a verification job was launched."""
        if not self._settings.verifier_enabled:
            return False
        try:
            return self._trigger(filename, fm)
        except Exception:
            log.exception("verification trigger failed for %s", filename)
            return False

    def should_verify(self, filename: str, fm: dict[str, Any]) -> bool:
        """SOTA check over the read model — same inputs as the leaderboard."""
        if fm.get("status") != "agent-run":
            return False
        score = _positive_number(fm.get(self._settings.score_field))
        if score is None:
            return False
        with self._lock:
            if filename in self._in_flight:
                return False
        index = self._read_model.verification_index()
        if index.get(filename) in (VALID, INVALID):
            return False
        champion = champion_score(
            self._settings, self._read_model.records("results"), index
        )
        return champion is None or self._settings.better(score, champion)

    def _trigger(self, filename: str, fm: dict[str, Any]) -> bool:
        if not self.should_verify(filename, fm):
            return False
        owner = agent_from_filename(filename) or str(fm.get("agent") or "")
        reported_score = float(fm[self._settings.score_field])
        resolved = self.resolve_submission(fm, owner)
        if resolved is None:
            log.info("result %s claims SOTA but its submission is unresolvable", filename)
            self._announce(filename, unreproducible_body(owner=owner, filename=filename))
            return False
        submission_bucket, submission_prefix = resolved

        with self._lock:
            if filename in self._in_flight:
                return False
            self._in_flight.add(filename)
        try:
            run_prefix = f"{self._settings.verification_runs_prefix}/{filename}"
            # Pre-create /state: an empty rw bucket-volume mount fails the job
            # with `init container exhausted retries`.
            self._write_run_file(
                f"{run_prefix}/verification_request.json",
                json.dumps(
                    {
                        "filename": filename,
                        "reported_score": reported_score,
                        "submission_bucket": submission_bucket,
                        "submission_prefix": submission_prefix,
                        "requested_at": stamp_iso(utc_now()),
                        "by": self._settings.verifier_agent,
                    },
                    indent=2,
                    sort_keys=True,
                ),
                best_effort=False,
            )
            job_id, job_url = self._runner.launch_verification(
                submission_bucket=submission_bucket,
                submission_prefix=submission_prefix,
                run_prefix=run_prefix,
                label=filename,
            )
        except BaseException:
            with self._lock:
                self._in_flight.discard(filename)
            raise
        log.info(
            "verification launched for %s: job=%s submission=%s/%s",
            filename, job_id, submission_bucket, submission_prefix,
        )
        self._spawn(
            f"verify-watch-{job_id}",
            lambda: self._watch(filename, owner, reported_score, run_prefix, job_id, job_url),
        )
        return True

    # ───────────────── submission resolution ─────────────────

    def resolve_submission(
        self, fm: dict[str, Any], owner: str
    ) -> tuple[str, str] | None:
        """Resolve the result's frontmatter to a runnable (bucket, prefix).

        A location that is a run-output dir (it carries ``run_request.json``
        from a self-run launcher or ``job_status.json`` from ``/v1/jobs:run``)
        is followed through its pointer back to the submission; otherwise any
        non-empty directory counts as the submission — the harness validates
        its contents and fails fast with logs the owner can read. None →
        unresolvable.
        """
        for bucket, prefix in self._candidate_locations(fm, owner):
            pointed = self._submission_from_run_dir(bucket, prefix)
            if pointed is not None:
                if self._has_submission(*pointed):
                    return pointed
                continue  # explicit pointer to nothing → not a submission
            if self._has_submission(bucket, prefix):
                return bucket, prefix
        return None

    def _candidate_locations(
        self, fm: dict[str, Any], owner: str
    ) -> list[tuple[str, str]]:
        values: list[str] = []
        for key in ("artifacts", "submission"):
            v = fm.get(key)
            if isinstance(v, str):
                values.append(v)
            elif isinstance(v, (list, tuple)):
                values.extend(str(x) for x in v)
        out: list[tuple[str, str]] = []
        for value in values:
            loc = self._parse_location(value, owner)
            if loc is not None and loc not in out:
                out.append(loc)
        return out

    def _parse_location(self, value: str, owner: str) -> tuple[str, str] | None:
        value = value.strip().strip("`").strip()
        if not value:
            return None
        if value.startswith("hf://"):
            parsed = parse_source_uri(value)
            if parsed is None:
                return None
            return f"{parsed.org}/{parsed.bucket}", parsed.path.strip("/")
        rel = value.strip("/")
        if rel.startswith("artifacts/"):
            return self._settings.central_bucket, rel
        # `submissions/...`, `results/...`, or any other relative path lives in
        # the owner's scratch bucket.
        return self._settings.agent_bucket(owner), rel

    def _has_submission(self, bucket: str, prefix: str) -> bool:
        """A runnable submission is any non-empty directory — the harness
        validates its contents and fails fast with logs the owner can read."""
        return bool(self._hub.list_bucket_dir(bucket, prefix.strip("/")))

    def _submission_from_run_dir(
        self, bucket: str, prefix: str
    ) -> tuple[str, str] | None:
        base = prefix.strip("/")
        rr_path = f"{base}/run_request.json"
        js_path = f"{base}/job_status.json"
        fetched = self._hub.download_many(bucket, [rr_path, js_path])
        rr = _parse_json_object(fetched.get(rr_path))
        if rr is not None and rr.get("submission_prefix"):
            return (
                str(rr.get("submission_bucket") or bucket),
                str(rr["submission_prefix"]).strip("/"),
            )
        js = _parse_json_object(fetched.get(js_path))
        if js is not None and js.get("submission_prefix"):
            return bucket, str(js["submission_prefix"]).strip("/")
        return None

    # ───────────────── verdict watcher ─────────────────

    def _watch(
        self,
        filename: str,
        owner: str,
        reported_score: float,
        run_prefix: str,
        job_id: str,
        job_url: str,
    ) -> None:
        try:
            status, stage, message = self._runner.watch_terminal(job_id)
            self._write_run_file(
                f"{run_prefix}/job_logs.txt", self._runner.fetch_logs_text(job_id)
            )
            self._write_run_file(
                f"{run_prefix}/job_status.json",
                json.dumps(
                    {
                        "status": status,
                        "stage": stage,
                        "message": message,
                        "job_id": job_id,
                        "job_url": job_url,
                        "filename": filename,
                        "finished_at": stamp_iso(utc_now()),
                    },
                    indent=2,
                    sort_keys=True,
                ),
            )
            if status != "completed":
                # Transient (error/timeout/cancel): leave `pending`, no
                # announcement; the reconciler or the next SOTA event retries.
                log.warning(
                    "verification job %s for %s ended %s (%s); leaving pending",
                    job_id, filename, status, message,
                )
                return
            raw = self._hub.read_audit_bytes(f"{run_prefix}/summary.json")
            if raw is None:
                log.warning(
                    "verification job %s for %s completed but wrote no "
                    "summary.json; leaving pending", job_id, filename,
                )
                return
            summary = json.loads(raw.decode("utf-8"))
            self.record_verdict(
                filename,
                owner=owner,
                reported_score=reported_score,
                summary=summary,
                job_id=job_id,
            )
        except Exception:
            log.exception(
                "verification watcher failed for %s (job %s); leaving pending",
                filename, job_id,
            )
        finally:
            with self._lock:
                self._in_flight.discard(filename)

    def record_verdict(
        self,
        filename: str,
        *,
        owner: str,
        reported_score: float,
        summary: dict[str, Any],
        job_id: str | None,
    ) -> str | None:
        """Verdict + CAS write + announcement, from a completed run's summary.

        Shared by the in-Space watcher and the offline reconciler. Returns the
        verdict written, or None when undecidable / deferred / skipped.
        """
        s = self._settings
        verdict, details = compute_verdict(s, reported_score, summary)
        if verdict is None:
            log.warning(
                "summary for %s lacks usable numbers; leaving pending", filename
            )
            return None
        outcome = self._verification.set_verdict(
            filename,
            verdict,
            by=s.verifier_agent,
            details={**details, "reported_score": reported_score, "job_id": job_id},
        )
        log.info("verdict for %s: %s (%s)", filename, verdict, outcome)
        if outcome != WRITTEN:
            return None
        # The Space just rewrote the index itself; don't let reads (and the
        # next SOTA check's champion) wait out the listing TTL to see it.
        self._read_model.invalidate_verification_index()
        self._announce(
            filename,
            verdict_body(
                s,
                owner=owner,
                filename=filename,
                verdict=verdict,
                reported_score=reported_score,
                details=details,
            ),
        )
        return verdict

    # ───────────────── helpers ─────────────────

    def _announce(self, filename: str, body: str) -> None:
        try:
            msg_filename, recipients = post_server_message(
                settings=self._settings,
                hub=self._hub,
                read_model=self._read_model,
                agent_id=self._settings.verifier_agent,
                body=body,
                refs=[filename],
            )
            log.info(
                "announced verification of %s as %s (delivered to %s)",
                filename, msg_filename, recipients,
            )
        except Exception:
            log.exception("verification announcement failed for %s", filename)

    def _write_run_file(self, path: str, text: str, *, best_effort: bool = True) -> None:
        try:
            self._hub.write_bytes_audit(path, text.encode("utf-8"))
        except Exception:
            if not best_effort:
                raise
            log.exception("failed to write %s to the audit bucket", path)


def _parse_json_object(raw: bytes | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
