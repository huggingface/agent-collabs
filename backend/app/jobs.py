"""Launch and supervise the benchmark HF Job.

The benchmark runs entirely on platform-mounted bucket volumes (/submission,
/harness, /state) — the job needs no token inside the container. The org-credits
token is used ONLY to launch (and bill) the job via ``run_job``; it never enters
the job, so participant-supplied code cannot read it.

The harness contract (documented in challenge.yaml): the challenge author
uploads a directory to ``{central_bucket}/{HARNESS_PREFIX}`` containing
``{JOB_HARNESS_ENTRYPOINT}``. The job runs::

    python3 /harness/{entrypoint} --submission-dir /submission \
        --state-dir /state [--private-dir /private] {JOB_EXTRA_ARGS...}

and must write ``/state/summary.json`` containing at least
``{"<SCORE_FIELD>": <number>}`` (plus the guard field if the verifier guard is
configured). Everything else — datasets, serving, measurement — is the
harness's business; the Space never reads the submission.

A background watcher thread enforces the runtime cap independently of the
platform's own ``timeoutSeconds``: when the job ends (or overruns the cap) it
cancels any still-running job, fetches the logs, and writes ``job_logs.txt`` plus
``job_status.json`` into the participant's ``run_prefix`` so they can debug
without managing the job themselves.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from huggingface_hub import HfApi, JobStage, Volume, run_job
from huggingface_hub.errors import HfHubHTTPError

from app.config import Settings
from app.errors import JobLaunchFailed
from app.hub import HubClient
from app.naming import stamp_iso, utc_now


log = logging.getLogger(__name__)

_TERMINAL_STAGES = {
    JobStage.COMPLETED.value,
    JobStage.CANCELED.value,
    JobStage.ERROR.value,
    JobStage.DELETED.value,
}

# Maps the terminal job stage to the status we report to the participant.
_STATUS_FOR_STAGE = {
    JobStage.COMPLETED.value: "completed",
    JobStage.CANCELED.value: "canceled",
    JobStage.ERROR.value: "error",
    JobStage.DELETED.value: "deleted",
}


def _stage_value(stage: Any) -> str:
    return getattr(stage, "value", str(stage))


def slug_from_prefix(prefix: str) -> str:
    slug = prefix.strip("/").replace("/", "-").replace("_", "-")
    return "".join(ch for ch in slug if ch.isalnum() or ch == "-")[:80] or "submission"


def sanitize_label(value: str) -> str:
    """HF Jobs tags allow only [A-Za-z0-9_=-]; result filenames contain '.'."""
    return re.sub(r"[^A-Za-z0-9_=-]", "-", value)[:80] or "unnamed"


class JobRunner:
    def __init__(self, settings: Settings, hub: HubClient):
        self._settings = settings
        self._hub = hub
        self._api = HfApi()

    def _harness_command(self, *, private: bool) -> list[str]:
        s = self._settings
        cmd = [
            "python3",
            f"/harness/{s.harness_entrypoint}",
            "--submission-dir", "/submission",
            "--state-dir", "/state",
        ]
        if private:
            cmd += ["--private-dir", "/private"]
        cmd += s.job_extra_arg_list
        return cmd

    # ───────────────────────── public ─────────────────────────

    def launch_benchmark(
        self,
        *,
        agent_id: str,
        hf_user: str,
        bucket: str,
        submission_prefix: str,
        run_prefix: str,
    ) -> tuple[str, str]:
        """Launch the benchmark job and start its watcher. Returns (job_id, job_url)."""
        s = self._settings
        timeout = f"{s.job_timeout_minutes}m"

        volumes = [
            Volume(
                type="bucket",
                source=bucket,
                path=submission_prefix,
                mount_path="/submission",
                read_only=True,
            ),
            Volume(
                type="bucket",
                source=s.central_bucket,
                path=s.harness_prefix,
                mount_path="/harness",
                read_only=True,
            ),
            Volume(
                type="bucket",
                source=bucket,
                path=run_prefix,
                mount_path="/state",
                read_only=False,
            ),
        ]

        labels = {
            "task": f"{s.collab_slug}-benchmark",
            "submission": slug_from_prefix(submission_prefix),
            "agent_id": agent_id,
            "via": "bucket-sync-api",
        }

        try:
            # No `secrets`: the job needs no in-job token (platform-mounted
            # volumes). The admin token only authorizes the launch; the job
            # runs under the org namespace so org credits pay.
            job = run_job(
                image=s.job_image,
                command=self._harness_command(private=False),
                flavor=s.job_flavor,
                timeout=timeout,
                labels=labels,
                volumes=volumes,
                namespace=s.org,
                token=s.resolved_token(),
            )
        except HfHubHTTPError as exc:
            raise JobLaunchFailed(str(exc))

        self._write_status(
            bucket,
            run_prefix,
            {
                "status": "running",
                "stage": _stage_value(job.status.stage),
                "job_id": job.id,
                "job_url": job.url,
                "agent_id": agent_id,
                "hf_user": hf_user,
                "submission_prefix": submission_prefix,
                "timeout_minutes": s.job_timeout_minutes,
                "launched_at": stamp_iso(utc_now()),
            },
        )

        threading.Thread(
            target=self._watch,
            args=(job.id, job.url, agent_id, hf_user, bucket, run_prefix, submission_prefix),
            name=f"job-watch-{job.id}",
            daemon=True,
        ).start()

        return job.id, job.url

    def launch_verification(
        self,
        *,
        submission_bucket: str,
        submission_prefix: str,
        run_prefix: str,
        label: str,
    ) -> tuple[str, str]:
        """Launch the private-set verification job. Returns (job_id, job_url).

        Same canonical harness as ``launch_benchmark`` so the verdict measures
        exactly what participants measure, with two differences: the private
        eval set from the audit bucket is mounted ro at /private, and the rw
        /state lives in the audit bucket too — private data may echo into the
        job output, so it must never be participant-readable. The caller
        pre-creates ``run_prefix`` (an empty rw bucket-volume mount fails with
        `init container exhausted retries`). No watcher is spawned here — the
        verifier supervises via ``watch_terminal``.
        """
        s = self._settings
        volumes = [
            Volume(
                type="bucket",
                source=submission_bucket,
                path=submission_prefix,
                mount_path="/submission",
                read_only=True,
            ),
            Volume(
                type="bucket",
                source=s.central_bucket,
                path=s.harness_prefix,
                mount_path="/harness",
                read_only=True,
            ),
            Volume(
                type="bucket",
                source=s.audit_bucket,
                path=s.private_dataset_prefix,
                mount_path="/private",
                read_only=True,
            ),
            Volume(
                type="bucket",
                source=s.audit_bucket,
                path=run_prefix,
                mount_path="/state",
                read_only=False,
            ),
        ]

        labels = {
            "task": f"{s.collab_slug}-verification",
            "result": sanitize_label(label),
            "via": "bucket-sync-verifier",
        }

        try:
            # Same safety property as launch_benchmark: no `secrets` — the
            # admin token only authorizes the launch and never enters the
            # container.
            job = run_job(
                image=s.job_image,
                command=self._harness_command(private=True),
                flavor=s.job_flavor,
                timeout=f"{s.job_timeout_minutes}m",
                labels=labels,
                volumes=volumes,
                namespace=s.org,
                token=s.resolved_token(),
            )
        except HfHubHTTPError as exc:
            raise JobLaunchFailed(str(exc))
        return job.id, job.url

    # ───────────────────────── watcher ─────────────────────────

    def watch_terminal(self, job_id: str) -> tuple[str, str | None, str]:
        """Poll until the job is terminal (or cap+grace → cancel).

        Returns (status, stage, message) with status one of
        completed/canceled/error/deleted/timed_out/unknown. Shared by the
        benchmark watcher and the verifier's verdict watcher.
        """
        s = self._settings
        ns = s.org
        tok = s.resolved_token()
        # Allow a small grace beyond the platform's own cap before we force-cancel,
        # so a job that the platform is already tearing down is not double-killed.
        deadline = time.monotonic() + s.job_timeout_minutes * 60 + s.job_watch_grace_s

        final_stage: str | None = None
        timed_out = False
        last_message = ""
        while True:
            try:
                info = self._api.inspect_job(job_id=job_id, namespace=ns, token=tok)
                final_stage = _stage_value(info.status.stage)
                last_message = info.status.message or ""
            except Exception as exc:  # transient API hiccup; keep polling
                log.debug("inspect_job(%s) failed: %s", job_id, exc)
                final_stage = None

            if final_stage in _TERMINAL_STAGES:
                break
            if time.monotonic() > deadline:
                timed_out = True
                try:
                    self._api.cancel_job(job_id=job_id, namespace=ns, token=tok)
                except Exception as exc:
                    log.warning("cancel_job(%s) failed: %s", job_id, exc)
                break
            time.sleep(s.job_watch_poll_s)

        if timed_out:
            return (
                "timed_out",
                final_stage,
                f"job did not finish within {s.job_timeout_minutes} minutes and was "
                f"stopped; see job_logs.txt for partial output",
            )
        status = _STATUS_FOR_STAGE.get(final_stage or "", "unknown")
        message = {
            "completed": "benchmark completed; see summary.json",
            "error": f"job ended in error: {last_message}".strip(": ").strip(),
            "canceled": "job was canceled",
            "deleted": "job was deleted",
        }.get(status, f"job ended in stage '{final_stage}'")
        return status, final_stage, message

    def _watch(
        self,
        job_id: str,
        job_url: str,
        agent_id: str,
        hf_user: str,
        bucket: str,
        run_prefix: str,
        submission_prefix: str,
    ) -> None:
        s = self._settings
        status, final_stage, message = self.watch_terminal(job_id)

        self._write_logs(bucket, run_prefix, job_id)

        self._write_status(
            bucket,
            run_prefix,
            {
                "status": status,
                "stage": final_stage,
                "job_id": job_id,
                "job_url": job_url,
                "agent_id": agent_id,
                "hf_user": hf_user,
                "submission_prefix": submission_prefix,
                "timeout_minutes": s.job_timeout_minutes,
                "finished_at": stamp_iso(utc_now()),
                "message": message,
            },
        )
        log.info("job %s finished: status=%s stage=%s", job_id, status, final_stage)

    # ───────────────────────── helpers ─────────────────────────

    def _write_status(self, bucket: str, run_prefix: str, payload: dict[str, Any]) -> None:
        try:
            self._hub.write_text_to_bucket(
                bucket,
                f"{run_prefix.strip('/')}/job_status.json",
                json.dumps(payload, indent=2, sort_keys=True),
            )
        except Exception:
            log.exception("failed to write job_status.json to %s/%s", bucket, run_prefix)

    def fetch_logs_text(self, job_id: str) -> str:
        """The job's log tail as one string; errors degrade to a placeholder."""
        try:
            lines = list(
                self._api.fetch_job_logs(
                    job_id=job_id,
                    namespace=self._settings.org,
                    follow=False,
                    tail=self._settings.job_log_tail_lines,
                    token=self._settings.resolved_token(),
                )
            )
            return "\n".join(line.rstrip("\n") for line in lines) or "(no logs returned)"
        except Exception as exc:
            log.warning("fetch_job_logs(%s) failed: %s", job_id, exc)
            return f"(failed to fetch logs: {exc})"

    def _write_logs(self, bucket: str, run_prefix: str, job_id: str) -> None:
        text = self.fetch_logs_text(job_id)
        try:
            self._hub.write_text_to_bucket(
                bucket, f"{run_prefix.strip('/')}/job_logs.txt", text
            )
        except Exception:
            log.exception("failed to write job_logs.txt to %s/%s", bucket, run_prefix)
