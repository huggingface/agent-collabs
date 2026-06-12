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
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import Settings
from app.frontmatter import parse
from app.hub import HubClient, ListedFile
from app.naming import VERIFICATION_STATUS_PATH


log = logging.getLogger(__name__)

_README_RE = re.compile(r"(?:^|/)README\.md$", re.IGNORECASE)

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
        entries = self._md_entries(folder)
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
        return [out[p] for p in sorted(out)]

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

    def write_through(self, path: str, frontmatter: dict, body: str, size: int) -> None:
        """Insert a just-written central-bucket file so read-after-write is
        exact regardless of listing TTL. Call right after the bucket write."""
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
