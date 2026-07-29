from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Response

from app.errors import NotFound


router = APIRouter()

# clients/collab_watch.sh sits next to app/ both locally (backend/clients/) and
# in the Docker image (/app/clients/), so resolving relative to this package
# file lands on it in either layout.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "clients" / "collab_watch.sh"


@router.get("/v1/watch.sh")
def watch_script() -> Response:
    """Serve clients/collab_watch.sh so any agent can fetch its own watcher
    straight from the backend it already talks to: `curl -fsS <base>/v1/watch.sh
    -o watch.sh && sh watch.sh <base> <you>`.

    Read from disk on every request rather than baked in at import: the script
    is the client contract, and a redeploy that ships a new one must serve it
    without anyone remembering to bump a constant."""
    try:
        data = _SCRIPT_PATH.read_bytes()
    except OSError:
        raise NotFound(str(_SCRIPT_PATH))
    return Response(content=data, media_type="text/x-shellscript")
