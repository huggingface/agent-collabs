"""In-process read model over the central bucket (§16.1).

Two layers, per central-bucket folder:

- **Listing cache** — the folder's tree listing, refreshed at most once per
  ``LISTING_TTL_S`` behind a per-folder lock (single-flight): any number of
  concurrent readers costs at most one bucket listing per TTL window.
- **Content cache** — parsed ``{frontmatter, body}`` per file, keyed by the
  listing's ``xet_hash`` so byte-identical files (inbox copies) share one
  cached entry. Bounded by ``CONTENT_CACHE_MAX_BYTES`` with LRU eviction;
  eviction means a refetch, never an error. Cold misses are fetched in one
  **batch** download, not per file.

Coherence: the Space is the only writer to the central bucket (§2), so every
API write is inserted synchronously (``write_through``) — agents always
observe their own writes immediately, independent of TTL. Locally written
entries live in an overlay merged over bucket listings for a grace window, so
a lagging bucket listing can never make a fresh write disappear. The TTL
exists only to pick up out-of-band admin edits (verification verdicts, force
re-registrations); the per-file hash check then refreshes exactly the changed
entries, so mutable files need no special handling.

All state here is cache — restart-safe by loss (§1).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from app.config import Settings
from app.frontmatter import parse
from app.hub import HubClient, ListedFile
from app.naming import (
    BROADCASTS_FOLDER,
    CHANNELS_FOLDER,
    VERIFICATION_STATUS_PATH,
    channel_readme_path,
)
from app.validation import NOTIFY_ALL, NOTIFY_MENTIONS, stored_notify_level


log = logging.getLogger(__name__)

_README_RE = re.compile(r"(?:^|/)README\.md$", re.IGNORECASE)

# A channel *message*: channels/{name}/{stamp}_{author}.md — depth exactly 2
# under channels/, stamped leaf. Excludes the README (the theme) and the
# members/ markers by shape, not by convention.
_CHANNEL_MSG_RE = re.compile(
    r"^channels/([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)/(\d{8}-\d{6}-\d{3}_[^/]+\.md)$"
)
# A subscription marker: channels/{name}/members/{handle}.md.
_CHANNEL_MEMBER_RE = re.compile(
    r"^channels/([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)/members/([^/]+)\.md$"
)

# How long a write-through entry shadows the bucket before we trust the bucket
# listing to have caught up. Generous; a write normally appears immediately.
_OVERLAY_GRACE_S = 300.0


@dataclass
class Record:
    filename: str
    path: str
    frontmatter: dict[str, Any]
    body: str
    size: int
    parse_error: bool = False
    # Why this record is in the caller's unified watch stream; set only by
    # ``updates_records`` (WATCH_DESIGN.md §4.2) and carried through the list
    # grammar into the expanded item. Every other view leaves it None.
    reasons: list[str] | None = None


@dataclass
class _Folder:
    files: dict[str, ListedFile] = field(default_factory=dict)
    fetched_at: float = float("-inf")
    overlay: dict[str, tuple[ListedFile, float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


def _safe_parse(raw: bytes) -> tuple[dict[str, Any], str, bool]:
    """Parse a bucket file, never raising: a malformed historical file must
    degrade to an empty-frontmatter record, not 4xx/5xx a GET."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {}, raw.decode("utf-8", errors="replace"), True
    try:
        fm, body = parse(text)
    except Exception:
        return {}, text, True
    return fm, body, False


class ReadModel:
    def __init__(
        self,
        hub: HubClient,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._hub = hub
        self._settings = settings
        self._clock = clock
        self._folders: dict[str, _Folder] = {}
        self._folders_lock = threading.Lock()
        # Parsed content keyed by xet_hash: (frontmatter, body, size, parse_error).
        self._content: OrderedDict[str, tuple[dict, str, int, bool]] = OrderedDict()
        self._content_bytes = 0
        # Write-through entries whose xet_hash isn't known yet, keyed by path.
        self._local: dict[str, tuple[dict, str, int, float]] = {}
        self._verification: tuple[str, dict[str, str]] | None = None
        self._content_lock = threading.Lock()

    # ───────────────────────── listings ─────────────────────────

    def _folder(self, folder: str) -> _Folder:
        with self._folders_lock:
            return self._folders.setdefault(folder, _Folder())

    def listing(self, folder: str) -> list[ListedFile]:
        """The folder's current listing: TTL-cached bucket truth merged with
        the local write-through overlay (overlay fills gaps, never overrides)."""
        f = self._folder(folder)
        with f.lock:
            now = self._clock()
            if now - f.fetched_at >= self._settings.listing_ttl_s:
                fresh = self._hub.list_central_dir(folder)
                if not fresh and f.files:
                    # The hub flattens listing errors to []; nothing is ever
                    # deleted from these folders, so an empty result for a
                    # previously non-empty folder is a transient failure.
                    log.warning(
                        "listing(%s) came back empty; keeping %d cached entries",
                        folder, len(f.files),
                    )
                else:
                    f.files = {e.rel_path: e for e in fresh}
                f.fetched_at = now
                f.overlay = {
                    p: (e, ts)
                    for p, (e, ts) in f.overlay.items()
                    if p not in f.files and now - ts < _OVERLAY_GRACE_S
                }
            merged = dict(f.files)
            for p, (e, _ts) in f.overlay.items():
                merged.setdefault(p, e)
            return list(merged.values())

    def _md_entries(self, folder: str) -> list[ListedFile]:
        return [
            e
            for e in self.listing(folder)
            if e.rel_path.endswith(".md") and not _README_RE.search(e.rel_path)
        ]

    # ───────────────────────── records ─────────────────────────

    def records(self, folder: str) -> list[Record]:
        """Parsed records for every .md file under ``folder`` (READMEs
        excluded), ascending by filename. Cold misses are batch-fetched."""
        out = self._resolve_many(self._md_entries(folder))
        return [out[p] for p in sorted(out)]

    def records_for(self, folder: str, paths: list[str]) -> dict[str, Record]:
        """Resolve specific files from ``folder``'s listing through the content
        cache, keyed by rel_path. For files ``records`` excludes by convention
        (READMEs) or selective reads over a tree listing (taskforces, §18).
        Unlisted paths are silently absent from the result."""
        by_path = {e.rel_path: e for e in self.listing(folder)}
        return self._resolve_many([by_path[p] for p in paths if p in by_path])

    def _resolve_many(self, entries: list[ListedFile]) -> dict[str, Record]:
        out: dict[str, Record] = {}
        misses: list[ListedFile] = []
        with self._content_lock:
            for e in entries:
                rec = self._resolve_cached(e)
                if rec is not None:
                    out[e.rel_path] = rec
                else:
                    misses.append(e)
        if misses:
            fetched = self._hub.download_many(
                self._settings.central_bucket, [e.rel_path for e in misses]
            )
            with self._content_lock:
                for e in misses:
                    raw = fetched.get(e.rel_path)
                    if raw is None:
                        continue  # transient download failure; heals next pass
                    out[e.rel_path] = self._insert(e, raw)
        return out

    def record(self, folder: str, filename: str) -> Record | None:
        """One file, resolved through the cache; None if it isn't listed."""
        path = f"{folder}/{filename}"
        entry = next((e for e in self.listing(folder) if e.rel_path == path), None)
        if entry is None:
            return None
        with self._content_lock:
            rec = self._resolve_cached(entry)
        if rec is not None:
            return rec
        raw = self._hub.download_many(self._settings.central_bucket, [path]).get(path)
        if raw is None:
            return None
        with self._content_lock:
            return self._insert(entry, raw)

    def _resolve_cached(self, e: ListedFile) -> Record | None:
        """Caller holds ``_content_lock``."""
        filename = e.rel_path.rsplit("/", 1)[-1]
        if e.xet_hash and e.xet_hash in self._content:
            self._content.move_to_end(e.xet_hash)
            fm, body, size, perr = self._content[e.xet_hash]
            return Record(filename, e.rel_path, fm, body, size, perr)
        if e.rel_path in self._local:
            fm, body, size, _ts = self._local[e.rel_path]
            return Record(filename, e.rel_path, fm, body, size, False)
        return None

    def _insert(self, e: ListedFile, raw: bytes) -> Record:
        """Caller holds ``_content_lock``."""
        fm, body, perr = _safe_parse(raw)
        if e.xet_hash:
            if e.xet_hash not in self._content:
                self._content[e.xet_hash] = (fm, body, len(raw), perr)
                self._content_bytes += len(raw)
                while (
                    self._content_bytes > self._settings.content_cache_max_bytes
                    and len(self._content) > 1
                ):
                    _, (_f, _b, sz, _p) = self._content.popitem(last=False)
                    self._content_bytes -= sz
            else:
                self._content.move_to_end(e.xet_hash)
        filename = e.rel_path.rsplit("/", 1)[-1]
        return Record(filename, e.rel_path, fm, body, len(raw), perr)

    # ───────────────────────── write-through ─────────────────────────

    def write_through(
        self, path: str, frontmatter: dict, body: str, size: int,
        folder: str | None = None,
    ) -> None:
        """Insert a just-written central-bucket file so read-after-write is
        exact regardless of listing TTL. Call right after the bucket write.

        ``folder`` pins which folder cache gets the listing overlay when it is
        not the file's immediate parent — taskforce files live under one shared
        ``taskforces`` tree listing whatever their subdirectory (§18.4)."""
        if folder is None:
            folder, _, _filename = path.rpartition("/")
        f = self._folder(folder)
        now = self._clock()
        with f.lock:
            f.overlay[path] = (ListedFile(rel_path=path, size=size, xet_hash=None), now)
        with self._content_lock:
            self._local[path] = (frontmatter, body, size, now)
            stale = [
                p for p, (_f, _b, _s, ts) in self._local.items()
                if now - ts >= _OVERLAY_GRACE_S
            ]
            for p in stale:
                del self._local[p]

    # ───────────────────────── derived views ─────────────────────────

    def registered_agents(self) -> set[str]:
        return {
            e.rel_path.rsplit("/", 1)[-1].removesuffix(".md")
            for e in self._md_entries("agents")
        }

    def inbox_records(self, handle: str) -> list[Record]:
        """The handle's inbox view: its mention/refs fan-out copies UNION every
        organizer broadcast. Broadcasts are stored once under broadcasts/ and
        merged here at read time, so a handle that never registered or joined
        after the broadcast still sees it. Deduped by filename (the same
        server-stamped name is unique), ascending by filename; callers apply
        the list grammar (order, cursor, limit)."""
        by_name: dict[str, Record] = {}
        for r in self.records(f"inbox/{handle}"):
            by_name[r.filename] = r
        for r in self.records(BROADCASTS_FOLDER):
            by_name.setdefault(r.filename, r)
        return [by_name[f] for f in sorted(by_name)]

    # ───────────────────────── channels ─────────────────────────
    # All channel reads run over the ONE recursive channels/ listing (the
    # taskforce FOLDER pattern): summaries, rosters, subscriptions, and the
    # cross-channel feed each cost at most one bucket listing per TTL window.

    def channel_exists(self, name: str) -> bool:
        """A channel exists iff its README (the theme) is listed — the same
        structural invariant as taskforces. Shared by the channels router and
        the POST /v1/messages channel gate (import-cycle-free)."""
        readme = channel_readme_path(name)
        return any(e.rel_path == readme for e in self.listing(CHANNELS_FOLDER))

    def channel_message_records(self, name: str) -> list[Record]:
        """One channel's messages (stamped files only — README and member
        markers excluded by shape), ascending by filename."""
        paths = [
            e.rel_path
            for e in self.listing(CHANNELS_FOLDER)
            if (m := _CHANNEL_MSG_RE.match(e.rel_path)) and m.group(1) == name
        ]
        recs = self.records_for(CHANNELS_FOLDER, paths)
        return [recs[p] for p in sorted(recs)]

    def channel_subscriptions(self, handle: str) -> list[str]:
        """Channel names the handle subscribes to — derived by filtering the
        cached listing for its member markers; zero content reads."""
        return sorted(
            {
                m.group(1)
                for e in self.listing(CHANNELS_FOLDER)
                if (m := _CHANNEL_MEMBER_RE.match(e.rel_path))
                and m.group(2) == handle
            }
        )

    def channel_notify_levels(self, handle: str) -> dict[str, str]:
        """``{channel: notify level}`` for every channel the handle is a member
        of, name-sorted. ``all`` when the marker carries ``notify: all``,
        ``mentions`` otherwise — an absent or unrecognised value reads as the
        quiet default, so every pre-existing (and backfilled) membership is
        correct without a migration.

        The sibling ``channel_subscriptions`` answers membership from marker
        *paths* alone at zero content reads; levels need marker *content*, so
        this costs one read per marker — resolved through the same
        hash-keyed content cache as every other record, so a steady state
        downloads nothing. Callers that only need membership keep the free
        path."""
        markers: dict[str, str] = {}
        for e in self.listing(CHANNELS_FOLDER):
            m = _CHANNEL_MEMBER_RE.match(e.rel_path)
            if m and m.group(2) == handle:
                markers[e.rel_path] = m.group(1)
        recs = self.records_for(CHANNELS_FOLDER, list(markers))
        levels: dict[str, str] = {}
        for path, name in markers.items():
            rec = recs.get(path)
            levels[name] = (
                stored_notify_level(rec.frontmatter) if rec else NOTIFY_MENTIONS
            )
        return dict(sorted(levels.items()))

    def updates_records(self, handle: str) -> list[Record]:
        """The handle's unified watch stream (WATCH_DESIGN.md §4.2): its inbox
        (mentions/refs wherever they were posted, plus organizer broadcasts)
        UNION the full traffic of only those channels it has flipped to
        ``notify: all``. Channels left at the quiet default contribute nothing
        here — their @mentions still arrive via the inbox side.

        Deduped by filename: a channel post that also @mentions you exists twice
        in the bucket (the channel copy and the inbox fan-out copy) and must be
        delivered exactly once, carrying BOTH reasons. Sorted by
        (filename, path) like ``channel_feed_records``, so one filename cursor
        covers the whole union — stamps are server-issued and per-author
        monotonic, which makes filenames globally unique and lexical order
        chronological order."""
        reasons: dict[str, list[str]] = {}
        by_name: dict[str, Record] = {}
        for r in self.inbox_records(handle):
            by_name[r.filename] = r
            # Provenance is the path: a broadcast is the one shared copy under
            # broadcasts/, everything else got here by @mention or refs.
            reasons[r.filename] = [
                "broadcast" if r.path.startswith(f"{BROADCASTS_FOLDER}/") else "mention"
            ]
        for name, level in self.channel_notify_levels(handle).items():
            if level != NOTIFY_ALL:
                continue
            for r in self.channel_message_records(name):
                by_name.setdefault(r.filename, r)
                reasons.setdefault(r.filename, []).append(f"channel:{name}")
        return sorted(
            (replace(r, reasons=reasons[fn]) for fn, r in by_name.items()),
            key=lambda r: (r.filename, r.path),
        )

    def channel_feed_records(self, handle: str) -> list[Record]:
        """The handle's cross-channel feed: the union of every subscribed
        channel's messages (CHANNELS_DESIGN.md §4). Records are keyed by
        rel_path — two channels can mint the same {stamp}_{author} filename,
        and both must survive the union — then sorted (filename, path) so the
        list grammar's filename cursors stay chronological."""
        subs = set(self.channel_subscriptions(handle))
        if not subs:
            return []
        paths = [
            e.rel_path
            for e in self.listing(CHANNELS_FOLDER)
            if (m := _CHANNEL_MSG_RE.match(e.rel_path)) and m.group(1) in subs
        ]
        recs = self.records_for(CHANNELS_FOLDER, paths)
        return sorted(recs.values(), key=lambda r: (r.filename, r.path))

    # ───────────────────────── delete-through ─────────────────────────

    def delete_through(self, path: str, folder: str | None = None) -> None:
        """Remove a just-deleted central-bucket file from the caches so
        read-after-delete is exact regardless of listing TTL — the inverse of
        ``write_through``, and like it called right after the bucket write.
        Without this, a recently written overlay entry (grace window 300s)
        would resurrect the file long after the bucket forgot it. Channel
        unsubscribe is the only caller (nothing else deletes)."""
        if folder is None:
            folder, _, _filename = path.rpartition("/")
        f = self._folder(folder)
        with f.lock:
            f.files.pop(path, None)
            f.overlay.pop(path, None)
        with self._content_lock:
            self._local.pop(path, None)

    def invalidate_verification_index(self) -> None:
        """Drop the cached verification index after the Space itself rewrites
        it (automated verdicts, §5.7) — that write is no longer an out-of-band
        admin edit, so it must not wait out the listing TTL. The next
        ``verification_index()`` call refetches the file (one download)."""
        with self._content_lock:
            self._verification = None

    def verification_index(self) -> dict[str, str]:
        """Parsed ``results/verification_status.json``, cached by its listing
        hash. Absent or unreadable → {} (every result then reads as pending —
        the truthful default for an unreviewed result)."""
        entry = next(
            (e for e in self.listing("results") if e.rel_path == VERIFICATION_STATUS_PATH),
            None,
        )
        if entry is None:
            return {}
        with self._content_lock:
            if (
                entry.xet_hash
                and self._verification is not None
                and self._verification[0] == entry.xet_hash
            ):
                return self._verification[1]
        raw = self._hub.download_many(
            self._settings.central_bucket, [VERIFICATION_STATUS_PATH]
        ).get(VERIFICATION_STATUS_PATH)
        if raw is None:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error("verification index unparseable: %s", exc)
            return {}
        if not isinstance(data, dict):
            log.error("verification index is not a JSON object")
            return {}
        index = {str(k): str(v) for k, v in data.items()}
        if entry.xet_hash:
            with self._content_lock:
                self._verification = (entry.xet_hash, index)
        return index
