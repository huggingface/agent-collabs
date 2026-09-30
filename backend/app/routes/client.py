from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response

from app.errors import NotFound


router = APIRouter()

# clients/ sits next to app/ both locally (backend/clients/) and in the Docker
# image (/app/clients/), so resolving relative to this package file lands on it
# in either layout.
_CLIENTS_DIR = Path(__file__).resolve().parent.parent.parent / "clients"
_SCRIPT_PATH = _CLIENTS_DIR / "collab_watch.sh"
_SHARE_TRACE_PATH = _CLIENTS_DIR / "share_trace.py"


def _serve(path: Path, media_type: str) -> Response:
    try:
        data = path.read_bytes()
    except OSError:
        raise NotFound(str(path))
    return Response(content=data, media_type=media_type)


@router.get("/v1/watch.sh")
def watch_script() -> Response:
    """Serve clients/collab_watch.sh so any agent can fetch its own watcher
    straight from the backend it already talks to: `curl -fsS <base>/v1/watch.sh
    -o watch.sh && sh watch.sh <base> <you>`.

    Read from disk on every request rather than baked in at import: the script
    is the client contract, and a redeploy that ships a new one must serve it
    without anyone remembering to bump a constant."""
    return _serve(_SCRIPT_PATH, "text/x-shellscript")


@router.get("/v1/share_trace.py")
def share_trace_script() -> Response:
    """Serve clients/share_trace.py the same way: `curl -fsS <base>/v1/share_trace.py
    -o share_trace.py && python3 share_trace.py`."""
    return _serve(_SHARE_TRACE_PATH, "text/x-python")
