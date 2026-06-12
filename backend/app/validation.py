from __future__ import annotations

from app.config import Settings
from app.errors import InvalidPath
from app.naming import (
    AGENT_ID_RE,
    SLUG_RE,
    SourceURI,
    agent_id_from_bucket,
    parse_source_uri,
)


BLOCKED_TARGETS = {
    "README.md",
    "LEADERBOARD.md",
    "shared_resources/README.md",
}
BLOCKED_PREFIXES = ("audit/", "inbox/")

# The human-* namespace identifies human participants in inbox routing
# (§16.4): @human-<name> delivers without a registration check, so no agent
# may register inside it (bare "human" included, so it can't be squatted
# either — it routes nowhere).
HUMAN_HANDLE_PREFIX = "human-"


def is_human_handle(handle: str) -> bool:
    return handle.startswith(HUMAN_HANDLE_PREFIX) and len(handle) > len(HUMAN_HANDLE_PREFIX)


def validate_agent_id(agent_id: str) -> None:
    if agent_id != agent_id.lower():
        raise InvalidPath(
            f"agent_id must be lowercase: {agent_id!r}",
            hint=f"use '{agent_id.lower()}' instead",
        )
    if not AGENT_ID_RE.match(agent_id):
        raise InvalidPath(f"invalid agent_id: {agent_id!r}")


def validate_registerable_agent_id(agent_id: str) -> None:
    """Format check plus the reserved-namespace check — registration only.

    Read paths (inbox, digest) accept human-* handles, so they use the plain
    format check; minting an identity must not be able to squat the namespace.
    """
    validate_agent_id(agent_id)
    if agent_id == "human" or agent_id.startswith(HUMAN_HANDLE_PREFIX):
        raise InvalidPath(
            f"agent_id '{agent_id}' is reserved: 'human-<name>' handles identify "
            "human participants in inbox routing",
            hint="pick an agent_id that does not start with 'human-'",
        )


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise InvalidPath(f"invalid slug: {slug!r}")


def validate_path_components(path: str) -> None:
    if not path:
        raise InvalidPath("empty path")
    if path.startswith("/"):
        raise InvalidPath("path must not be absolute")
    for part in path.rstrip("/").split("/"):
        if part in ("", ".", ".."):
            raise InvalidPath(f"invalid path component: {part!r}")
        if part.startswith("."):
            raise InvalidPath(f"path component must not start with '.': {part!r}")
        if any(ord(c) < 32 for c in part):
            raise InvalidPath("path contains control characters")


def check_dest_not_blocked(target: str) -> None:
    norm = target.lstrip("/")
    if norm in BLOCKED_TARGETS:
        raise InvalidPath(f"target path blocked: {norm}", hint="this path is reserved")
    for prefix in BLOCKED_PREFIXES:
        if norm.startswith(prefix):
            raise InvalidPath(f"target path blocked: {norm}", hint=f"prefix '{prefix}' is reserved")


def resolve_source(settings: Settings, source: str) -> tuple[SourceURI, str]:
    """Parse a source URI and confirm it points inside a valid agent bucket.

    Returns (parsed_uri, agent_id). Raises InvalidPath otherwise.
    """
    parsed = parse_source_uri(source)
    if parsed is None:
        raise InvalidPath(f"source must be an hf://buckets/... URI, got: {source!r}")
    if parsed.org != settings.org:
        raise InvalidPath(
            f"source must be under org '{settings.org}', got '{parsed.org}'",
            hint="agents post from buckets in this org only",
        )
    agent_id = agent_id_from_bucket(parsed.bucket, settings.collab_slug)
    if agent_id is None:
        raise InvalidPath(
            f"source bucket '{parsed.bucket}' does not match '{settings.collab_slug}-<agent_id>'",
            hint="source must be under your own scratch bucket",
        )
    if parsed.path:
        validate_path_components(parsed.path)
    return parsed, agent_id


def validate_shared_dest_path(dest_path: str, agent_id: str) -> None:
    validate_path_components(dest_path)
    leaf = dest_path.rsplit("/", 1)[-1]
    marker = f"_{agent_id}"
    leaf_no_ext = leaf.rsplit(".", 1)[0]
    if marker not in leaf_no_ext and marker not in dest_path:
        raise InvalidPath(
            f"shared_resources dest path must include '_{agent_id}' in the leaf component",
            hint=f"e.g. 'tokenizers/{agent_id}_bpe.json' or 'plots/curve_{agent_id}.png'",
        )
    full_target = f"shared_resources/{dest_path}"
    check_dest_not_blocked(full_target)
