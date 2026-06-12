from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict


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
