"""Wrapper over huggingface_hub's bucket API."""
from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from huggingface_hub import (
    batch_bucket_files,
    bucket_info,
    download_bucket_files,
    list_bucket_tree,
    whoami,
)
from huggingface_hub.errors import (
    EntryNotFoundError,
    HfHubHTTPError,
    RepositoryNotFoundError,
)

from app.config import Settings
from app.naming import SourceURI, parse_source_uri


log = logging.getLogger(__name__)


@dataclass
class ListedFile:
    rel_path: str
    size: int
    xet_hash: str | None = None


class HubClient:
    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def _token(self) -> str:
        return self._settings.resolved_token()

    # ───────────────────────── Bucket existence & identity ─────────────────────────

    def bucket_exists(self, bucket: str) -> bool:
        try:
            bucket_info(bucket, token=self._token)
            return True
        except (RepositoryNotFoundError, HfHubHTTPError) as e:
            log.debug("bucket_exists(%s) -> False (%s)", bucket, e)
            return False

    def bucket_author(self, bucket: str) -> str | None:
        """Return the `author` field from BucketInfo (the org name for org buckets).

        Not a true creator-of-record; for identity binding we rely on the
        whoami-at-registration flow plus a handshake file in the agent's bucket.
        """
        try:
            info = bucket_info(bucket, token=self._token)
        except (RepositoryNotFoundError, HfHubHTTPError) as e:
            log.debug("bucket_author(%s) failed: %s", bucket, e)
            return None
        return getattr(info, "author", None)

    def whoami_for_token(self, token: str) -> str:
        info = whoami(token=token)
        if isinstance(info, dict) and info.get("name"):
            return info["name"]
        raise ValueError("whoami did not return a `name` field")

    def whoami_user_and_orgs(self, token: str) -> tuple[str, set[str]]:
        """Resolve a caller token to (hf_user, org names). Used by the human
        message path (§5.4a), where org membership gates the write."""
        info = whoami(token=token)
        if not isinstance(info, dict) or not info.get("name"):
            raise ValueError("whoami did not return a `name` field")
        orgs = {
            o["name"]
            for o in (info.get("orgs") or [])
            if isinstance(o, dict) and o.get("name")
        }
        return info["name"], orgs

    # ───────────────────────── Reads ─────────────────────────

    def read_bytes(self, uri: SourceURI | str) -> bytes:
        parsed = uri if isinstance(uri, SourceURI) else parse_source_uri(uri)
        if parsed is None:
            raise ValueError(f"invalid source URI: {uri}")
        bucket = f"{parsed.org}/{parsed.bucket}"
        return self._download_one(bucket, parsed.path)

    def read_text(self, uri: SourceURI | str) -> str:
        return self.read_bytes(uri).decode("utf-8")

    def read_central_bytes(self, target_path: str) -> bytes:
        return self._download_one(self._settings.central_bucket, target_path)

    def read_central_text(self, target_path: str) -> str:
        return self.read_central_bytes(target_path).decode("utf-8")

    def read_central_bytes_optional(self, target_path: str) -> bytes | None:
        """Read a central-bucket file, distinguishing a genuinely missing file
        (returns None) from a transport/HTTP error (propagates).

        Unlike ``read_central_bytes`` — which flattens both cases to
        ``FileNotFoundError`` — this lets read-modify-write callers fail SAFE on
        a storage blip: skip the update rather than overwrite a live file with a
        fresh, near-empty one. Mirrors ``read_audit_bytes`` for the central bucket.
        """
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "f"
            try:
                download_bucket_files(
                    bucket_id=self._settings.central_bucket,
                    files=[(target_path, str(local))],
                    raise_on_missing_files=True,
                    token=self._token,
                )
            except EntryNotFoundError:
                return None
            return local.read_bytes()

    def _download_one(self, bucket: str, remote_path: str) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "f"
            try:
                download_bucket_files(
                    bucket_id=bucket,
                    files=[(remote_path, str(local))],
                    raise_on_missing_files=True,
                    token=self._token,
                )
            except (EntryNotFoundError, HfHubHTTPError) as e:
                raise FileNotFoundError(f"{bucket}/{remote_path}: {e}")
            return local.read_bytes()

    def download_many(self, bucket: str, remote_paths: list[str]) -> dict[str, bytes]:
        """Batch-download files, returning {remote_path: bytes}.

        Missing or failed entries are simply absent from the result — callers
        (the read model, the backfill script) treat absence as transient and
        retry on a later pass. Chunked so a multi-thousand-file cold fill
        doesn't ride on a single oversized call.
        """
        out: dict[str, bytes] = {}
        chunk_size = 500
        for start in range(0, len(remote_paths), chunk_size):
            chunk = remote_paths[start : start + chunk_size]
            with tempfile.TemporaryDirectory() as td:
                pairs = [(remote, str(Path(td) / str(i))) for i, remote in enumerate(chunk)]
                try:
                    download_bucket_files(
                        bucket_id=bucket,
                        files=pairs,
                        raise_on_missing_files=False,
                        token=self._token,
                    )
                except (EntryNotFoundError, HfHubHTTPError) as e:
                    log.warning(
                        "download_many(%s, %d files) failed: %s", bucket, len(chunk), e
                    )
                    continue
                for remote, local in pairs:
                    p = Path(local)
                    if p.exists():
                        out[remote] = p.read_bytes()
        return out

    def list_central_dir(self, prefix: str) -> list[ListedFile]:
        return self._list(self._settings.central_bucket, prefix)

    def list_bucket_dir(self, bucket: str, prefix: str) -> list[ListedFile]:
        return self._list(bucket, prefix)

    def _list(self, bucket: str, prefix: str) -> list[ListedFile]:
        out: list[ListedFile] = []
        try:
            for entry in list_bucket_tree(
                bucket_id=bucket,
                prefix=prefix or None,
                recursive=True,
                token=self._token,
            ):
                if getattr(entry, "type", None) == "file":
                    out.append(
                        ListedFile(
                            rel_path=entry.path,
                            size=entry.size or 0,
                            xet_hash=getattr(entry, "xet_hash", None),
                        )
                    )
        except (RepositoryNotFoundError, HfHubHTTPError) as e:
            log.debug("list(%s, %s) failed: %s", bucket, prefix, e)
        return out

    # ───────────────────────── Writes (central bucket) ─────────────────────────

    def write_bytes_central(self, target_path: str, data: bytes) -> None:
        batch_bucket_files(
            bucket_id=self._settings.central_bucket,
            add=[(data, target_path)],
            token=self._token,
        )

    def write_text_central(self, target_path: str, text: str) -> None:
        self.write_bytes_central(target_path, text.encode("utf-8"))

    def write_many_central(self, items: list[tuple[bytes, str]]) -> None:
        """Write several central-bucket files in one batch call.

        Used to land a message and its inbox fan-out copies together (§16.4):
        one storage round trip, no window where board and inbox diverge.
        """
        if not items:
            return
        batch_bucket_files(
            bucket_id=self._settings.central_bucket,
            add=list(items),
            token=self._token,
        )

    def write_bytes_to_bucket(self, bucket: str, target_path: str, data: bytes) -> None:
        batch_bucket_files(bucket_id=bucket, add=[(data, target_path)], token=self._token)

    def write_text_to_bucket(self, bucket: str, target_path: str, text: str) -> None:
        self.write_bytes_to_bucket(bucket, target_path, text.encode("utf-8"))

    def append_jsonl_audit(self, target_path: str, line: str) -> None:
        """Append to the audit log in the private (out-of-org) audit bucket."""
        self._append_jsonl(self._settings.audit_bucket, target_path, line)

    def read_audit_bytes(self, target_path: str) -> bytes | None:
        """Read a file from the private audit bucket.

        Returns the bytes, or None if the file genuinely does not exist. Unlike
        read_central_bytes, transport/HTTP errors PROPAGATE rather than being
        flattened to "missing" — so callers (e.g. the job quota) can fail closed
        on a storage outage instead of treating it as an empty ledger.
        """
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "f"
            try:
                download_bucket_files(
                    bucket_id=self._settings.audit_bucket,
                    files=[(target_path, str(local))],
                    raise_on_missing_files=True,
                    token=self._token,
                )
            except EntryNotFoundError:
                return None
            return local.read_bytes()

    def write_bytes_audit(self, target_path: str, data: bytes) -> None:
        batch_bucket_files(
            bucket_id=self._settings.audit_bucket,
            add=[(data, target_path)],
            token=self._token,
        )

    def _append_jsonl(self, bucket: str, target_path: str, line: str) -> None:
        try:
            existing = self._download_one(bucket, target_path)
        except FileNotFoundError:
            existing = b""
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        batch_bucket_files(
            bucket_id=bucket,
            add=[(existing + line.encode("utf-8") + b"\n", target_path)],
            token=self._token,
        )

    # ───────────────────────── Cross-bucket copy ─────────────────────────

    def copy_file_to_central(self, src_bucket: str, src_xet_hash: str, dest_path: str) -> None:
        """Hash-copy a single source file into the central bucket (bytes never
        transit the Space). The caller passes the source's xet hash — taken from a
        listing it already holds — so there is no extra lookup here."""
        if not src_xet_hash:
            raise RuntimeError(f"missing xet_hash for copy to {dest_path}")
        batch_bucket_files(
            bucket_id=self._settings.central_bucket,
            copy=[("bucket", src_bucket, src_xet_hash, dest_path)],
            token=self._token,
        )

    def copy_tree_to_central(
        self, src_bucket: str, src_prefix: str, dest_prefix: str
    ) -> Iterable[tuple[str, str, int]]:
        files = self._list(src_bucket, src_prefix)
        if not files:
            return
        prefix = src_prefix.rstrip("/")
        copy_ops: list[tuple[str, str, str, str]] = []
        results: list[tuple[str, str, int]] = []
        for f in files:
            if not f.xet_hash:
                raise RuntimeError(f"missing xet_hash for source file: {f.rel_path}")
            rel = f.rel_path[len(prefix) + 1 :] if prefix and f.rel_path.startswith(prefix + "/") else f.rel_path
            dest_path = f"{dest_prefix.rstrip('/')}/{rel}"
            copy_ops.append(("bucket", src_bucket, f.xet_hash, dest_path))
            results.append((f.rel_path, dest_path, f.size))

        batch_bucket_files(
            bucket_id=self._settings.central_bucket,
            copy=copy_ops,
            token=self._token,
        )
        for r in results:
            yield r
