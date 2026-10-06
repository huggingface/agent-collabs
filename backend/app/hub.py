"""Wrapper over huggingface_hub's bucket API."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:  # huggingface_hub 2.x ships its own httpx fork; 1.x uses httpx itself.
    import httpx2 as httpx  # type: ignore[import-not-found]
except ImportError:
    import httpx  # httpx is not in requirements.txt: it comes with huggingface_hub
from huggingface_hub import (
    batch_bucket_files,
    bucket_info,
    create_bucket,
    download_bucket_files,
    list_bucket_tree,
    whoami,
)
from huggingface_hub.constants import ENDPOINT
from huggingface_hub.errors import (
    EntryNotFoundError,
    HfHubHTTPError,
    RepositoryNotFoundError,
)
from huggingface_hub.utils import build_hf_headers, get_session

from app.caller_write import classify as classify_caller_write_error
from app.config import Settings
from app.naming import SourceURI, parse_source_uri


log = logging.getLogger(__name__)


@dataclass
class ListedFile:
    rel_path: str
    size: int
    xet_hash: str | None = None


@dataclass(frozen=True)
class HubIdentity:
    username: str
    orgs: set[str]
    email: str | None = None


@dataclass(frozen=True)
class OrgMemberRole:
    user: str
    role: str


class HubUnreachable(Exception):
    """The Hub answered 5xx/429 or not at all: transient, safe to retry."""


def _status(e: Exception) -> int | None:
    resp = getattr(e, "response", None)
    return getattr(resp, "status_code", None)


def _transient(e: Exception) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    status = _status(e)
    return isinstance(e, HfHubHTTPError) and (status is None or status >= 500 or status == 429)


def _raise_caller_write_error(e: Exception) -> None:
    """A Hub call refused for a caller's token raises PermissionError; an
    outage raises HubUnreachable; anything else returns for the caller to
    re-raise. Uses the caller-write child's classifier, so a refusal is only
    ever an explicit 401/403."""
    if classify_caller_write_error(e)["result"] == "forbidden":
        raise PermissionError(f"the caller's token was refused (HTTP {_status(e) or '401/403'})") from e
    if isinstance(e, ConnectionError) or _transient(e):
        raise HubUnreachable(type(e).__name__) from e


# ── Caller-token bucket writes run in a child process (see app/caller_write.py)
_CALLER_WRITE_CMD = [sys.executable, "-m", "app.caller_write"]
_CALLER_WRITE_CWD = Path(__file__).resolve().parent.parent  # the dir holding app/
CALLER_WRITE_TIMEOUT_S = 60.0
# Registration is limited to a few per minute per user, so a handful of slots
# is plenty; a request that cannot get one in time gets a 503, not a pile-up.
CALLER_WRITE_SLOTS = threading.BoundedSemaphore(4)
CALLER_WRITE_SLOT_WAIT_S = 20.0
_ADMIN_TOKEN_VARS = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACEHUB_API_TOKEN")


def _caller_write_env() -> dict[str, str]:
    """The Space's environment minus its admin credential: the child acts only
    with the token it is handed on stdin."""
    env = {k: v for k, v in os.environ.items() if k not in _ADMIN_TOKEN_VARS}
    env["HF_TOKEN_PATH"] = os.devnull  # no fallback to a stored token file
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    return env


def _run_caller_write(bucket: str, path: str, text: str, token: str) -> dict:
    """Run one caller-token write in a child and return its structured result.
    Timeouts, crashes and unreadable output come back as "failed"."""
    if not CALLER_WRITE_SLOTS.acquire(timeout=CALLER_WRITE_SLOT_WAIT_S):
        log.warning("caller write: no free slot (bucket=%s)", bucket)
        return {"result": "failed", "type": "NoSlot", "status": None}
    try:
        try:
            proc = subprocess.run(
                _CALLER_WRITE_CMD,
                input=json.dumps({"token": token, "bucket": bucket, "path": path, "text": text}),
                capture_output=True, text=True, timeout=CALLER_WRITE_TIMEOUT_S,
                cwd=_CALLER_WRITE_CWD, env=_caller_write_env(),
            )
        except subprocess.TimeoutExpired:
            log.warning("caller write: timed out after %ss (bucket=%s)", CALLER_WRITE_TIMEOUT_S, bucket)
            return {"result": "failed", "type": "Timeout", "status": None}
    finally:
        CALLER_WRITE_SLOTS.release()
    # The child's stderr is never logged: only its size, for diagnosis.
    lines = proc.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1])
        if result.get("result") not in ("ok", "forbidden", "failed"):
            raise ValueError
    except (IndexError, ValueError, AttributeError):
        log.warning(
            "caller write: no result from child (bucket=%s exit=%s stderr_bytes=%d)",
            bucket, proc.returncode, len(proc.stderr or ""),
        )
        return {"result": "failed", "type": "ChildCrashed", "status": None}
    return result


class HubClient:
    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def _token(self) -> str:
        return self._settings.resolved_token()

    # ───────────────────────── Bucket existence & identity ─────────────────────────

    def bucket_exists(self, bucket: str) -> bool:
        """Only a 404 means "no such bucket"; a Hub outage raises
        HubUnreachable instead of looking like a missing bucket."""
        try:
            bucket_info(bucket, token=self._token)
            return True
        except (HfHubHTTPError, httpx.TransportError) as e:
            if isinstance(e, RepositoryNotFoundError) or _status(e) == 404:
                return False
            if _transient(e):
                raise HubUnreachable(str(e)) from e
            raise

    def create_bucket_as(self, bucket: str, token: str) -> None:
        """Create `bucket` with the caller's token so the caller owns it (only
        its creator and org admins can then write there). Default visibility.
        Raises PermissionError if the token may not create it."""
        try:
            create_bucket(bucket, exist_ok=True, token=token)
        except (HfHubHTTPError, httpx.TransportError, ConnectionError) as e:
            _raise_caller_write_error(e)
            raise

    def caller_may_write(self, bucket: str, token: str) -> bool | None:
        """Ask the Hub for a bucket write token with the caller's token, over
        plain HTTP (the request batch_bucket_files makes first). False on
        401/403, True on success, None when it can't tell. Only a pre-check
        for clean errors: the write itself remains the ownership proof. The
        response carries a Xet access token, so only its status is read."""
        url = f"{ENDPOINT}/api/buckets/{bucket}/xet-write-token"
        try:
            resp = get_session().get(url, headers=build_hf_headers(token=token), timeout=10)
        except Exception as e:
            log.warning("caller write pre-check failed (bucket=%s type=%s)", bucket, type(e).__name__)
            return None
        if resp.status_code in (401, 403):
            return False
        return True if 200 <= resp.status_code < 300 else None

    def write_text_as(self, bucket: str, path: str, text: str, token: str) -> None:
        """Write one file with the caller's token. Raises PermissionError when
        the Hub refuses (the bucket is not the caller's), HubUnreachable for
        anything else. The upload runs in a child process: a refused Xet
        upload poisons the process-wide Xet session (app/caller_write.py)."""
        if self.caller_may_write(bucket, token) is False:
            raise PermissionError(f"the caller's token may not write {bucket}")
        result = _run_caller_write(bucket, path, text, token)
        if result["result"] == "ok":
            return
        if result["result"] == "forbidden":
            raise PermissionError(f"the caller's token may not write {bucket} (HTTP {result.get('status')})")
        log.warning(
            "caller write failed (bucket=%s type=%s status=%s)",
            bucket, result.get("type"), result.get("status"),
        )
        raise HubUnreachable(f"caller write to {bucket} failed")

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

    def whoami_identity(self, token: str) -> HubIdentity:
        """Resolve a caller token to HF identity facts used by human posts.

        OAuth tokens expose org membership but not roleInOrg. When the Space's
        OAuth app requests the email scope, the email lets the organizer gate
        perform a targeted org-member lookup instead of scanning the full org.
        """
        try:
            info = whoami(token=token)
        except (HfHubHTTPError, httpx.TransportError) as e:
            if _transient(e):
                raise HubUnreachable(str(e)) from e
            raise
        if not isinstance(info, dict) or not info.get("name"):
            raise ValueError("whoami did not return a `name` field")
        orgs = {
            o["name"]
            for o in (info.get("orgs") or [])
            if isinstance(o, dict) and o.get("name")
        }
        email = info.get("email")
        if not isinstance(email, str) or not email.strip():
            email = None
        return HubIdentity(info["name"], orgs, email.strip() if email else None)

    def whoami_user_and_orgs(self, token: str) -> tuple[str, set[str]]:
        """Resolve a caller token to (hf_user, org names). Used by existing
        identity gates that do not need optional email."""
        identity = self.whoami_identity(token)
        return identity.username, identity.orgs

    def org_member_role_by_email(self, org: str, email: str) -> OrgMemberRole | None:
        """Resolve one org member by email and return its role, if available.

        The members endpoint supports an email filter for orgs with a matching
        Organization email domain or SSO allowed domain. This is the scalable
        organizer check path: one request for the caller instead of listing
        every org member. Transport errors propagate so callers can fall back
        or fail closed.
        """
        session = get_session()
        headers = build_hf_headers(token=self._token)
        url = f"{ENDPOINT}/api/organizations/{org}/members"
        resp = session.get(
            url,
            headers=headers,
            params={"email": email, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        page = resp.json()
        if not isinstance(page, list) or not page:
            return None
        member = page[0]
        if not isinstance(member, dict):
            return None
        user, role = member.get("user"), member.get("role")
        if not isinstance(user, str) or not isinstance(role, str):
            return None
        return OrgMemberRole(user=user, role=role)

    def org_member_roles(self, org: str) -> dict[str, str]:
        """Map {lowercased username: org role} for every member of ``org``.

        whoami omits a caller's org role for OAuth tokens, so the organizer
        gate can't read it from the caller's own token; instead the Space
        looks the role up here with its admin token. Lists the members
        endpoint, following Link pagination and falling back to offset-style
        pagination if needed. Raises on transport error so the caller
        can fail closed rather than treat an outage as "not an organizer".
        """
        session = get_session()
        headers = build_hf_headers(token=self._token)
        url = f"{ENDPOINT}/api/organizations/{org}/members"
        page_size = 100
        roles: dict[str, str] = {}
        offset = 0
        params: dict[str, int] | None = {"limit": page_size, "offset": offset}
        while True:
            resp = session.get(
                url,
                headers=headers,
                params=params,
                timeout=10,
            )
            resp.raise_for_status()
            page = resp.json()
            if not isinstance(page, list) or not page:
                break
            before = len(roles)
            for m in page:
                if not isinstance(m, dict):
                    continue
                user, role = m.get("user"), m.get("role")
                if user and role:
                    roles[user.lower()] = role
            next_url = resp.links.get("next", {}).get("url")
            if next_url:
                url = next_url
                params = None
                continue
            if len(page) < page_size or len(roles) == before:
                break
            offset += page_size
            params = {"limit": page_size, "offset": offset}
        return roles

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
        return self._download_optional(self._settings.central_bucket, target_path)

    def read_text_optional(self, uri: SourceURI | str) -> str | None:
        """``read_central_bytes_optional`` for any bucket: None only when the
        file is genuinely missing; any other failure propagates."""
        parsed = uri if isinstance(uri, SourceURI) else parse_source_uri(uri)
        if parsed is None:
            raise ValueError(f"invalid source URI: {uri}")
        data = self._download_optional(f"{parsed.org}/{parsed.bucket}", parsed.path)
        return None if data is None else data.decode("utf-8")

    def _download_optional(self, bucket: str, remote_path: str) -> bytes | None:
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "f"
            try:
                download_bucket_files(
                    bucket_id=bucket,
                    files=[(remote_path, str(local))],
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

    def delete_central(self, target_path: str) -> None:
        """Delete one central-bucket file. The only deleting write in the
        system: channel unsubscribe removes the member marker
        (CHANNELS_DESIGN.md §3.3). Everything else stays append-only."""
        batch_bucket_files(
            bucket_id=self._settings.central_bucket,
            delete=[target_path],
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
