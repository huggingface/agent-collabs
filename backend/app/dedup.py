from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable, Iterator


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class PromotionLRU:
    """LRU mapping (content_hash, dest_folder) -> existing target filename.

    Used to make bucket-source promotions idempotent: the same bytes promoted
    to the same destination twice in a row returns the existing filename
    instead of creating a duplicate.
    """

    def __init__(self, max_entries: int):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max = max_entries
        self._data: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, content_hash: str, dest_folder: str) -> str | None:
        key = (content_hash, dest_folder)
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
            return None

    def record(self, content_hash: str, dest_folder: str, filename: str) -> None:
        key = (content_hash, dest_folder)
        with self._lock:
            self._data[key] = filename
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)


class RecentPosts:
    """LRU mapping a message-post key -> the response it got, so a client that
    retries a POST /v1/messages gets the original back instead of posting (and
    fanning out) twice. Keys are ``(agent_id, idempotency_key)`` or, for raw
    posts without one, the post's full identity read with a ``max_age_s``
    window. Process memory, like ``PromotionLRU``: it covers a client's retry,
    not a Space restart.

    A post runs inside ``claim(key)``, so a retry that arrives while the first
    request is still writing waits for it and replays its response instead of
    missing the cache and posting again."""

    def __init__(self, max_entries: int, clock: Callable[[], float] = time.monotonic):
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max = max_entries
        self._clock = clock
        self._data: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        # key -> [lock held by the request posting under it, requests holding
        # or waiting for it]; dropped when the last one leaves.
        self._in_flight: dict[tuple, list] = {}

    @contextmanager
    def claim(self, key: tuple, max_age_s: float | None = None) -> Iterator[Any | None]:
        """Serialize requests that share `key`. Yields the response a previous
        request recorded under it (replay it), or None: post, then ``record``
        before leaving the block. A request that fails records nothing, so the
        next one waiting posts in its place. Other keys never wait."""
        with self._lock:
            entry = self._in_flight.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield self.get(key, max_age_s)
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._in_flight[key]

    def get(self, key: tuple, max_age_s: float | None = None) -> Any | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is None:
                return None
            at, value = hit
            if max_age_s is not None and self._clock() - at > max_age_s:
                return None
            self._data.move_to_end(key)
            return value

    def record(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._data[key] = (self._clock(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)
