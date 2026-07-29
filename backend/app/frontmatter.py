from __future__ import annotations

import datetime
import io
from typing import Any

import yaml

from app.config import Settings
from app.errors import InvalidFrontmatter


_DELIM = "---"


def parse(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith(_DELIM):
        return {}, text
    rest = text[len(_DELIM):].lstrip("\n")
    end = rest.find(f"\n{_DELIM}")
    if end == -1:
        return {}, text
    fm_text = rest[:end]
    body = rest[end + len(_DELIM) + 1 :]
    if body.startswith("\n"):
        body = body[1:]
    try:
        data = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        raise InvalidFrontmatter(f"could not parse YAML frontmatter: {e}")
    if not isinstance(data, dict):
        raise InvalidFrontmatter("frontmatter must be a mapping")
    return data, body


def serialise(fm: dict[str, Any], body: str) -> str:
    buf = io.StringIO()
    buf.write(_DELIM)
    buf.write("\n")
    yaml.safe_dump(fm, buf, sort_keys=False, default_flow_style=False, allow_unicode=True)
    buf.write(_DELIM)
    buf.write("\n")
    if body:
        if not body.startswith("\n"):
            buf.write("\n")
        buf.write(body)
        if not body.endswith("\n"):
            buf.write("\n")
    return buf.getvalue()


def merge(client_fm: dict[str, Any], server_fm: dict[str, Any]) -> dict[str, Any]:
    """Server-stamped fields always win; client fields fill in the rest."""
    merged = dict(client_fm)
    merged.update(server_fm)
    return merged


# Exactly the frontmatter keys the system itself writes onto a message: `type`
# and `refs` come from the client, `agent`/`timestamp`/`via` are server-stamped
# over whatever arrived, and `broadcast`/`channel` are server-owned (the routes
# reject them from clients with a more specific error before we get here — they
# are listed so this set stays an honest inventory of what a message file can
# contain, and so re-posting a message the API itself served still round-trips).
#
# The allowlist exists because message frontmatter is author-controlled and ends
# up inside the very JSON a watcher parses. In eq2 the client scanned responses
# for `"filename":"..."` anywhere, so one post carrying a
# `filename: 99999999-…zzz.md` key could pin every watcher's cursor past all
# future mail, permanently. The client-side fix is to read only the top-level
# server-computed `cursor` (WATCH_DESIGN.md §4.4); this is the other half, and
# it is the half that holds even against a client that gets it wrong: no
# response-shaped name (`filename`, `cursor`, `next`, `watch`) can ever appear
# in serialised frontmatter.
#
# The key allowlist alone is not enough: `yaml.safe_load` happily turns a
# mapping-valued key (e.g. `type: {cursor: 99999999-…zzz.md}`) into a nested
# dict, and that dict's *keys* serialise as raw JSON object keys — untouched by
# the JSON-string-escaping that makes ordinary string values safe. So every
# frontmatter value must also be a YAML scalar (`refs` is the one exception,
# where a list of scalars is the client-facing shape); rejecting non-scalar
# values is what actually makes the "no response-shaped name can ever appear
# in serialised frontmatter" invariant hold, rather than just holding for
# top-level keys.
MESSAGE_FRONTMATTER_KEYS = frozenset(
    {"type", "refs", "agent", "timestamp", "via", "broadcast", "channel"}
)

# yaml.safe_load's scalar result types (it also produces datetime.date/
# datetime.datetime for bare-looking dates and timestamps, not just str).
_SCALAR_TYPES = (str, int, float, bool, type(None), datetime.date, datetime.datetime)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALAR_TYPES)


def validate_message_frontmatter(fm: dict[str, Any]) -> None:
    """Reject client-supplied message frontmatter outside the allowlist, naming
    the offending key (WATCH_DESIGN.md §5.5); also reject any value that is not
    a YAML scalar (`refs` may be a list of scalars) — see the module comment
    above for why non-scalar values are the other half of the vulnerability."""
    for key, value in fm.items():
        if key not in MESSAGE_FRONTMATTER_KEYS:
            raise InvalidFrontmatter(
                f"frontmatter key {key!r} is not allowed on a message; allowed "
                f"keys: {', '.join(sorted(MESSAGE_FRONTMATTER_KEYS))} — put "
                "anything else in the body"
            )
        values = value if key == "refs" and isinstance(value, (list, tuple)) else (value,)
        if not all(_is_scalar(v) for v in values):
            raise InvalidFrontmatter(
                f"frontmatter value for {key!r} must be a scalar; lists are "
                "allowed only for 'refs' and only of scalars"
            )


ALLOWED_RESULT_STATUS = {"agent-run", "negative"}


def validate_result_frontmatter(settings: Settings, fm: dict[str, Any]) -> None:
    """Validate against the challenge's configured result schema.

    The score field must be a positive number; `status` (when required) must
    be agent-run|negative; every other required field must be a non-empty
    string (or at least present, for non-string values).
    """
    for field in settings.required_result_field_list:
        if field not in fm:
            raise InvalidFrontmatter(f"result frontmatter missing required field: {field}")

    score_val = fm[settings.score_field]
    if isinstance(score_val, bool) or not isinstance(score_val, (int, float)) or score_val <= 0:
        raise InvalidFrontmatter(
            f"`{settings.score_field}` must be a positive number ({settings.score_unit})"
        )

    if "status" in settings.required_result_field_list:
        if fm["status"] not in ALLOWED_RESULT_STATUS:
            raise InvalidFrontmatter(
                f"`status` must be one of {sorted(ALLOWED_RESULT_STATUS)}, got {fm['status']!r}"
            )

    for field in settings.required_result_field_list:
        if field in (settings.score_field, "status"):
            continue
        val = fm[field]
        if isinstance(val, str) and not val.strip():
            raise InvalidFrontmatter(f"`{field}` must be a non-empty string")
