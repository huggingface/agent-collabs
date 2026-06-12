"""Board-message compose + inbox fan-out, shared online/offline (§16.4, §5.7).

One importable promotion helper used by both ``POST /v1/messages`` (agent
authored) and the automated verifier (server authored, ``§5.7``), so the two
paths cannot drift — the same pattern as ``app/mentions.py``.
"""
from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.frontmatter import merge, serialise
from app.hub import HubClient
from app.mentions import extract_recipients
from app.naming import inbox_path, message_path, stamp_yaml, utc_now
from app.read_model import ReadModel


def promote_message(
    *,
    settings: Settings,
    hub: HubClient,
    read_model: ReadModel,
    agent_id: str,
    fm: dict,
    body: str,
    now: datetime,
) -> tuple[str, str, list[str], int]:
    """Land the board file and its inbox fan-out copies (§16.4) in one batch
    write, then write-through the cache. Returns (target, filename,
    recipients, bytes)."""
    content = serialise(fm, body)
    content_bytes = content.encode("utf-8")
    target = message_path(agent_id, now)
    filename = target.rsplit("/", 1)[-1]
    recipients = extract_recipients(
        body=body,
        refs=fm.get("refs"),
        author=agent_id,
        registered=read_model.registered_agents(),
        cap=settings.mention_fanout_cap,
    )
    targets = [target] + [inbox_path(r, filename) for r in recipients]
    hub.write_many_central([(content_bytes, t) for t in targets])
    for t in targets:
        read_model.write_through(t, fm, body, len(content_bytes))
    return target, filename, recipients, len(content_bytes)


def post_server_message(
    *,
    settings: Settings,
    hub: HubClient,
    read_model: ReadModel,
    agent_id: str,
    body: str,
    type_: str = "verification",
    refs: list[str] | None = None,
) -> tuple[str, list[str]]:
    """Compose and land a server-authored board message (no HTTP round trip).

    The Space is the central writer, so it stamps the frontmatter itself
    (``agent``, ``timestamp``, ``via: server``) and reuses the existing mention
    fan-out so ``@<owner>`` lands in the owner's inbox. Returns
    (filename, recipients).
    """
    client_fm: dict = {"type": type_}
    if refs:
        client_fm["refs"] = refs
    now = utc_now()
    server_fm = {"agent": agent_id, "timestamp": stamp_yaml(now), "via": "server"}
    _target, filename, recipients, _nbytes = promote_message(
        settings=settings,
        hub=hub,
        read_model=read_model,
        agent_id=agent_id,
        fm=merge(client_fm, server_fm),
        body=body,
        now=now,
    )
    return filename, recipients
