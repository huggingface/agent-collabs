from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings


AGENT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")

_SOURCE_URI_RE = re.compile(r"^hf://buckets/(?P<org>[^/]+)/(?P<bucket>[^/]+)(?:/(?P<path>.*))?$")


@dataclass(frozen=True)
class SourceURI:
    org: str
    bucket: str
    path: str

    def join(self, *parts: str) -> "SourceURI":
        new_path = "/".join([self.path, *parts]).strip("/") if self.path else "/".join(parts).strip("/")
        return SourceURI(self.org, self.bucket, new_path)

    def __str__(self) -> str:
        if self.path:
            return f"hf://buckets/{self.org}/{self.bucket}/{self.path}"
        return f"hf://buckets/{self.org}/{self.bucket}"


def parse_source_uri(uri: str) -> SourceURI | None:
    m = _SOURCE_URI_RE.match(uri)
    if not m:
        return None
    return SourceURI(org=m["org"], bucket=m["bucket"], path=m["path"] or "")


def agent_id_from_bucket(bucket: str, collab_slug: str) -> str | None:
    prefix = f"{collab_slug}-"
    if not bucket.startswith(prefix):
        return None
    agent_id = bucket[len(prefix):]
    if not AGENT_ID_RE.match(agent_id):
        return None
    return agent_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp_str(dt: datetime) -> str:
    base = dt.strftime("%Y%m%d-%H%M%S")
    ms = f"{dt.microsecond // 1000:03d}"
    return f"{base}-{ms}"


def stamp_filename(agent_id: str, dt: datetime) -> str:
    return f"{stamp_str(dt)}_{agent_id}.md"


def stamp_yaml(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def stamp_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def message_path(agent_id: str, dt: datetime) -> str:
    return f"message_board/{stamp_filename(agent_id, dt)}"


def result_path(agent_id: str, dt: datetime) -> str:
    return f"results/{stamp_filename(agent_id, dt)}"


def inbox_path(agent_id: str, filename: str) -> str:
    """Fan-out copy of a board message, byte-identical, same filename (§16.4)."""
    return f"inbox/{agent_id}/{filename}"


BROADCASTS_FOLDER = "broadcasts"


def broadcast_path(filename: str) -> str:
    """One shared copy of an organizer broadcast, byte-identical to its board
    file. Stored once here, not fanned out; the inbox read-time union surfaces
    it to every handle, so lurkers and late-registered agents see it too."""
    return f"{BROADCASTS_FOLDER}/{filename}"


# ── Channels (topic rooms, CHANNELS_DESIGN.md) ──
CHANNELS_FOLDER = "channels"

# Static path segments under /v1/channels/ — a channel with one of these names
# would shadow a fixed route (GET /v1/channels/feed), so they can never be
# channel names.
RESERVED_CHANNEL_NAMES = frozenset({"feed"})


def channel_dir(name: str) -> str:
    return f"{CHANNELS_FOLDER}/{name}"


def channel_readme_path(name: str) -> str:
    """The channel's theme. A channel exists iff this file does."""
    return f"{CHANNELS_FOLDER}/{name}/README.md"


def channel_member_path(name: str, handle: str) -> str:
    """One marker file per subscription: subscribe = write it, unsubscribe =
    delete it. No shared roster file to read-modify-write, so concurrent
    subscribes cannot lose each other; rosters and "what does X follow" are
    derived by filtering the one cached channels/ listing."""
    return f"{CHANNELS_FOLDER}/{name}/members/{handle}.md"


def channel_message_path(name: str, agent_id: str, dt: datetime) -> str:
    return f"{CHANNELS_FOLDER}/{name}/{stamp_filename(agent_id, dt)}"


def agent_from_filename(filename: str) -> str | None:
    # message/result filenames: {YYYYMMDD-HHmmss-mmm}_{agent_id}.md
    # agent filenames: {agent_id}.md
    stem = filename.removesuffix(".md")
    if "_" in stem and stem.split("_", 1)[0][:8].isdigit():
        return stem.split("_", 1)[1]
    return stem


# Flat index mapping each promoted result's basename -> verification state
# (`pending` | `valid` | `invalid`); an absent entry reads as `pending`.
VERIFICATION_STATUS_PATH = "results/verification_status.json"


def registration_path(agent_id: str) -> str:
    return f"agents/{agent_id}.md"


def artifact_dest_dir(slug: str, agent_id: str) -> str:
    return f"artifacts/{slug}_{agent_id}/"


def audit_log_path(dt: datetime) -> str:
    return f"audit/{dt.strftime('%Y%m')}.jsonl"


# ── Trace & stats sharing (one record per session, see TRACES_DESIGN.md) ──
TRACES_FOLDER = "traces"


def trace_dir(agent_id: str, session_id: str) -> str:
    """A session's bundle dir in the central bucket: traces/<agent>/<session>/.
    Holds manifest.md (always) + the native session log(s) (share=full)."""
    return f"{TRACES_FOLDER}/{agent_id}/{session_id}"


def trace_manifest_path(agent_id: str, session_id: str) -> str:
    return f"{trace_dir(agent_id, session_id)}/manifest.md"


def split_trace_manifest_path(path: str) -> tuple[str, str] | None:
    """Inverse of ``trace_manifest_path``: traces/<agent>/<session>/manifest.md
    → (agent, session). None for anything else under traces/ (e.g. log files)."""
    parts = path.split("/")
    if len(parts) == 4 and parts[0] == TRACES_FOLDER and parts[3] == "manifest.md":
        return parts[1], parts[2]
    return None


def expected_agent_bucket(settings: Settings, agent_id: str) -> str:
    return settings.agent_bucket(agent_id)


def central_uri(settings: Settings, path: str) -> str:
    return f"hf://buckets/{settings.central_bucket}/{path.lstrip('/')}"
