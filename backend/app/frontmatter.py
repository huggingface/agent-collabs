from __future__ import annotations

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
