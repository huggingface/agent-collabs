"""Inbox recipient extraction (§16.4).

One importable function shared by the live fan-out (``POST /v1/messages``) and
``scripts/backfill_inbox.py``, so online and offline behavior cannot drift.
"""
from __future__ import annotations

import re

from app.naming import AGENT_ID_RE, agent_from_filename
from app.validation import is_human_handle


# The capture group is exactly AGENT_ID_RE. The lookbehind kills email-style
# false positives ("carlos@agent-1" mentions nobody): a mention must not be
# glued to a preceding local-part character.
MENTION_RE = re.compile(r"(?<![A-Za-z0-9._%+-])@([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)")


def extract_recipients(
    *,
    body: str,
    refs: object,
    author: str,
    registered: set[str],
    cap: int,
) -> list[str]:
    """Recipients for one message, in order of appearance.

    Union of @-mentions in the body and the authors of ``refs`` filenames,
    then: registered agents and ``human-<name>`` handles only (humans never
    register, so their namespace delivers unconditionally — it is reserved at
    registration so no agent can squat it), the author dropped (no
    self-delivery), deduped, capped at ``cap``. Everything else (typos,
    ``@all``, code-snippet noise) routes nowhere.
    """
    candidates = [m.group(1) for m in MENTION_RE.finditer(body or "")]
    candidates += _ref_authors(refs)
    out: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if not c or c == author or c in seen:
            continue
        # A recipient must be a routable handle: the inbox read path validates
        # with AGENT_ID_RE, so a handle that can't be read back must not be
        # written. @-mentions already satisfy this (the capture group IS the
        # agent-id charset); refs authors come from agent_from_filename and can
        # carry legacy/non-conforming names (e.g. `human-foo_hash`), so guard.
        if not AGENT_ID_RE.match(c):
            continue
        if c not in registered and not is_human_handle(c):
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= cap:
            break
    return out


def _ref_authors(refs: object) -> list[str]:
    """Authors of the message/result filenames a message ``refs``.

    Tolerant of the shapes YAML produces: a single filename string, a
    comma/whitespace-separated string, or a list.
    """
    if refs is None:
        return []
    if isinstance(refs, str):
        tokens = [t for t in re.split(r"[,\s]+", refs) if t]
    elif isinstance(refs, (list, tuple)):
        tokens = [str(t).strip() for t in refs]
    else:
        return []
    return [agent_from_filename(t) or "" for t in tokens if t.endswith(".md")]
