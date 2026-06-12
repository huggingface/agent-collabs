"""Eval Space — polls pending results and writes verdicts.

Runs PRIVATE in the admin org. A background loop fetches results whose
verification state is `pending` from the backend API, hands each one to the
organizer-implemented ``evaluate()`` in ``evaluator.py``, and records the
returned verdict in ``results/verification_status.json`` in the central
bucket — the same file a human would edit, so the backend (and dashboard)
pick verdicts up within their listing TTL with no coupling to this Space.

Config (env; set by bootstrap/init_challenge.py):
  BACKEND_API_URL   the bucket-sync Space, e.g. https://...-bucket-sync.hf.space
  CENTRAL_BUCKET    e.g. my-challenge/my-main-bucket
  EVAL_POLL_S       poll interval in seconds (default 60)
  HF_TOKEN          secret; must be able to write the central bucket

Safety properties:
  - Only entries currently `pending` (or absent) are ever (re)written — a
    human verdict in the index is never overwritten by this Space.
  - The index is re-read immediately before each write (read-modify-write of
    the single JSON file), and only the evaluated filename's entry changes.
  - ``evaluate()`` returning None leaves the result pending (e.g. needs a
    human look, or a transient failure) — it will be retried next poll.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from huggingface_hub import batch_bucket_files, download_bucket_files
from huggingface_hub.errors import EntryNotFoundError

import evaluator

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("eval-space")

API = os.environ.get("BACKEND_API_URL", "").rstrip("/")
BUCKET = os.environ.get("CENTRAL_BUCKET", "")
POLL_S = float(os.environ.get("EVAL_POLL_S", "60"))
TOKEN = os.environ.get("HF_TOKEN")
INDEX_PATH = "results/verification_status.json"

VALID, INVALID, PENDING = "valid", "invalid", "pending"

app = FastAPI(title="eval-space")
_stats = {"evaluated": 0, "valid": 0, "invalid": 0, "left_pending": 0, "errors": 0,
          "last_poll": None}


@app.get("/healthz")
@app.get("/")
def healthz() -> dict:
    return {"status": "ok", "configured": bool(API and BUCKET and TOKEN), **_stats}


def _read_index() -> dict | None:
    """The current index, or None when it cannot be SAFELY read (never write
    over an index we could not read — that would erase human verdicts)."""
    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / "f"
        try:
            download_bucket_files(
                bucket_id=BUCKET, files=[(INDEX_PATH, str(local))],
                raise_on_missing_files=True, token=TOKEN,
            )
        except EntryNotFoundError:
            return {}
        except Exception as exc:
            log.warning("index read failed: %s", exc)
            return None
        # Read INSIDE the with-block — the tempdir is deleted on exit.
        try:
            data = json.loads(local.read_bytes())
        except (json.JSONDecodeError, OSError) as exc:
            log.error("index unparseable (%s); refusing to write", exc)
            return None
    return data if isinstance(data, dict) else None


def _record_verdict(filename: str, verdict: str) -> bool:
    """CAS-style: re-read the index and write the verdict only if the entry is
    still pending/absent. Returns True when written."""
    index = _read_index()
    if index is None:
        return False
    if index.get(filename, PENDING) != PENDING:
        log.info("%s: index entry is %r (human-set?); leaving untouched",
                 filename, index[filename])
        return False
    index[filename] = verdict
    body = json.dumps(index, indent=2, sort_keys=True) + "\n"
    try:
        batch_bucket_files(BUCKET, add=[(body.encode(), INDEX_PATH)], token=TOKEN)
        return True
    except Exception as exc:
        log.warning("index write failed for %s: %s", filename, exc)
        return False


def _pending_results() -> list[dict]:
    r = httpx.get(
        f"{API}/v1/results",
        params={"verification": "pending", "expand": "true", "limit": 200, "order": "asc"},
        timeout=60,
    )
    r.raise_for_status()
    items = r.json().get("items", [])
    return [it for it in items if isinstance(it, dict)]


def _poll_once() -> None:
    for item in _pending_results():
        filename = item.get("filename", "")
        try:
            verdict = evaluator.evaluate(
                filename, item.get("frontmatter") or {}, item.get("body") or ""
            )
        except Exception:
            log.exception("evaluate(%s) raised; leaving pending", filename)
            _stats["errors"] += 1
            continue
        if verdict not in (VALID, INVALID):
            _stats["left_pending"] += 1
            continue
        if _record_verdict(filename, verdict):
            _stats["evaluated"] += 1
            _stats[verdict] += 1
            log.info("%s -> %s", filename, verdict)


def _loop() -> None:
    if not (API and BUCKET and TOKEN):
        log.error("missing BACKEND_API_URL / CENTRAL_BUCKET / HF_TOKEN; eval loop idle")
        return
    while True:
        try:
            _poll_once()
        except Exception:
            log.exception("poll failed; retrying next interval")
            _stats["errors"] += 1
        _stats["last_poll"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        time.sleep(POLL_S)


threading.Thread(target=_loop, name="eval-loop", daemon=True).start()
