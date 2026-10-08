"""Write one bucket file with a CALLER's token, in a short-lived child process.

Bucket uploads go through the Xet client, whose session is global to the
process. When a caller's token may not write the bucket, the upload fails with
a ConnectionError and the session keeps that error: every later Xet operation
in the process — the Space's own admin-token reads and writes included — then
raises ``RuntimeError: Previous task error: ...`` until a restart. A refused
caller write is an ordinary user error (a taken agent_id, a read-only token),
so it runs here instead, where a poisoned session dies with the child.

Protocol: the parent runs ``python -m app.caller_write`` with a minimal
environment (no admin ``HF_TOKEN``) and sends one JSON object on stdin:
``{"token", "bucket", "path", "text"}``. The child prints one JSON object on
stdout: ``{"result": "ok"}``, ``{"result": "forbidden", "status": 401|403}``
or ``{"result": "failed", "type": "<ExceptionType>", "status": <int|null>}``.
The token never appears in argv, files, stdout or stderr.

Kept free of ``app.*`` imports so the child starts without the Space's
settings or credentials.
"""
from __future__ import annotations

import json
import re
import sys

# The Xet client's refusal text, e.g. "HTTP status client error (403 Forbidden)".
# Matched on the explicit status phrase, never a bare 401/403 that could be
# part of a bucket name or URL.
_XET_REFUSED_RE = re.compile(r"HTTP status client error \((401|403) (Unauthorized|Forbidden)\)")


def xet_refusal_status(e: BaseException) -> int | None:
    """401 or 403 when the Xet client's error text states an explicit refusal;
    None otherwise. Only the number leaves this function, never the text."""
    m = _XET_REFUSED_RE.search(str(e))
    return int(m.group(1)) if m else None


def classify(e: BaseException) -> dict:
    """Structured result for a failed caller write: "forbidden" only for an
    explicit 401/403 from the Hub; anything else is "failed" (upstream)."""
    resp = getattr(e, "response", None)
    status = getattr(resp, "status_code", None)
    if status in (401, 403):
        return {"result": "forbidden", "status": status}
    if isinstance(e, ConnectionError):
        refused = xet_refusal_status(e)
        if refused:
            return {"result": "forbidden", "status": refused}
    return {"result": "failed", "type": type(e).__name__, "status": status}


def run(request: dict) -> dict:
    from huggingface_hub import batch_bucket_files

    try:
        batch_bucket_files(
            bucket_id=request["bucket"],
            add=[(request["text"].encode("utf-8"), request["path"])],
            token=request["token"],
        )
    except Exception as e:
        return classify(e)
    return {"result": "ok"}


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
    except ValueError:
        print(json.dumps({"result": "failed", "type": "BadRequest", "status": None}))
        return 0
    print(json.dumps(run(request)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
