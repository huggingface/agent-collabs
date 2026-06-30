"""In-memory stand-ins and seeding helpers shared by the test modules."""
from __future__ import annotations

import hashlib

from app.config import Settings
from app.frontmatter import serialise
from app.hub import HubIdentity, ListedFile, OrgMemberRole
from app.naming import SourceURI, parse_source_uri


class FakeHub:
    """In-memory HubClient: buckets are dicts of {path: bytes}; xet_hash is
    sha256 — content-derived, exactly the property the read model relies on."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self.buckets: dict[str, dict[str, bytes]] = {
            settings.central_bucket: {},
            settings.audit_bucket: {},
        }
        self.list_calls = 0
        self.download_calls = 0
        self.batch_writes: list[list[str]] = []
        self.fail_listings = False
        # Scripted whoami identity for token-authenticated paths
        # (registration handshake, human message posts).
        self.whoami_user = "test-user"
        self.whoami_email: str | None = "test-user@example.com"
        self.whoami_orgs: set[str] = {settings.org}
        self.whoami_fails = False
        # Scripted challenge-org member roles for the organizer-broadcast gate.
        self.org_roles: dict[str, str] = {}
        self.org_roles_by_email: dict[str, tuple[str, str]] = {}
        self.org_member_roles_fails = False
        self.org_member_role_by_email_fails = False
        self.org_member_roles_calls = 0
        self.org_member_role_by_email_calls = 0

    # ── helpers ──────────────────────────────────────────────────────
    def _central(self) -> dict[str, bytes]:
        return self.buckets[self._settings.central_bucket]

    def seed(self, path: str, text: str, bucket: str | None = None) -> None:
        b = bucket or self._settings.central_bucket
        self.buckets.setdefault(b, {})[path] = text.encode("utf-8")

    # ── HubClient surface used by the app ────────────────────────────
    def list_central_dir(self, prefix: str) -> list[ListedFile]:
        self.list_calls += 1
        if self.fail_listings:
            return []  # the real hub flattens listing errors to []
        p = prefix.rstrip("/") + "/" if prefix else ""
        return [
            ListedFile(
                rel_path=path,
                size=len(data),
                xet_hash=hashlib.sha256(data).hexdigest(),
            )
            for path, data in self._central().items()
            if path.startswith(p)
        ]

    def list_bucket_dir(self, bucket: str, prefix: str) -> list[ListedFile]:
        p = prefix.rstrip("/") + "/" if prefix else ""
        return [
            ListedFile(
                rel_path=path,
                size=len(data),
                xet_hash=hashlib.sha256(data).hexdigest(),
            )
            for path, data in self.buckets.get(bucket, {}).items()
            if path.startswith(p)
        ]

    def download_many(self, bucket: str, remote_paths: list[str]) -> dict[str, bytes]:
        self.download_calls += 1
        files = self.buckets.get(bucket, {})
        return {p: files[p] for p in remote_paths if p in files}

    def read_central_text(self, path: str) -> str:
        files = self._central()
        if path not in files:
            raise FileNotFoundError(path)
        return files[path].decode("utf-8")

    def read_central_bytes_optional(self, path: str) -> bytes | None:
        return self._central().get(path)

    def write_text_central(self, path: str, text: str) -> None:
        self._central()[path] = text.encode("utf-8")

    def write_bytes_central(self, path: str, data: bytes) -> None:
        self._central()[path] = data

    def write_many_central(self, items: list[tuple[bytes, str]]) -> None:
        self.batch_writes.append([p for _, p in items])
        for data, p in items:
            self._central()[p] = data

    def read_bytes(self, uri) -> bytes:
        parsed = uri if isinstance(uri, SourceURI) else parse_source_uri(uri)
        if parsed is None:
            raise ValueError(f"invalid source URI: {uri}")
        files = self.buckets.get(f"{parsed.org}/{parsed.bucket}", {})
        if parsed.path not in files:
            raise FileNotFoundError(str(uri))
        return files[parsed.path]

    def read_text(self, uri) -> str:
        return self.read_bytes(uri).decode("utf-8")

    def append_jsonl_audit(self, path: str, line: str) -> None:
        b = self.buckets[self._settings.audit_bucket]
        existing = b.get(path, b"")
        if existing and not existing.endswith(b"\n"):
            existing += b"\n"
        b[path] = existing + line.encode("utf-8") + b"\n"

    def read_audit_bytes(self, path: str) -> bytes | None:
        return self.buckets[self._settings.audit_bucket].get(path)

    def write_bytes_audit(self, path: str, data: bytes) -> None:
        self.buckets[self._settings.audit_bucket][path] = data

    def write_bytes_to_bucket(self, bucket: str, path: str, data: bytes) -> None:
        self.buckets.setdefault(bucket, {})[path] = data

    def write_text_to_bucket(self, bucket: str, path: str, text: str) -> None:
        self.write_bytes_to_bucket(bucket, path, text.encode("utf-8"))

    def copy_tree_to_central(self, src_bucket: str, src_prefix: str, dest_prefix: str):
        """Mirror of HubClient.copy_tree_to_central: hash-copy a prefix into the
        central bucket, yielding (src_rel_path, dest_path, size)."""
        prefix = src_prefix.rstrip("/")
        central = self._central()
        out = []
        for path, data in list(self.buckets.get(src_bucket, {}).items()):
            if prefix and not (path == prefix or path.startswith(prefix + "/")):
                continue
            rel = path[len(prefix) + 1 :] if prefix and path.startswith(prefix + "/") else path
            dest = f"{dest_prefix.rstrip('/')}/{rel}"
            central[dest] = data
            out.append((path, dest, len(data)))
        return out

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.buckets

    def whoami_for_token(self, token: str) -> str:
        return self.whoami_user

    def whoami_identity(self, token: str) -> HubIdentity:
        if self.whoami_fails:
            raise ValueError("whoami did not return a `name` field")
        return HubIdentity(
            username=self.whoami_user,
            orgs=set(self.whoami_orgs),
            email=self.whoami_email,
        )

    def whoami_user_and_orgs(self, token: str) -> tuple[str, set[str]]:
        identity = self.whoami_identity(token)
        return identity.username, identity.orgs

    def org_member_role_by_email(self, org: str, email: str) -> OrgMemberRole | None:
        self.org_member_role_by_email_calls += 1
        if self.org_member_role_by_email_fails:
            raise RuntimeError("member email lookup failed")
        member = self.org_roles_by_email.get(email.lower())
        if member is None:
            return None
        user, role = member
        return OrgMemberRole(user=user, role=role)

    def org_member_roles(self, org: str) -> dict[str, str]:
        self.org_member_roles_calls += 1
        if self.org_member_roles_fails:
            raise RuntimeError("members lookup failed")
        return {u.lower(): r for u, r in self.org_roles.items()}


class FakeJobRunner:
    """JobRunner stand-in for verifier tests: records launches, lets the test
    script the terminal outcome, and (via ``on_launch``) simulate what the
    finished job wrote to /state — e.g. seed ``summary.json``."""

    def __init__(self):
        self.launches: list[dict] = []
        self.terminal: tuple[str, str | None, str] = ("completed", "COMPLETED", "done")
        self.logs = "fake job logs"
        self.on_launch = None  # callable(launch_dict) -> None

    def launch_verification(self, **kwargs) -> tuple[str, str]:
        job_id = f"job-{len(self.launches) + 1}"
        launch = {**kwargs, "job_id": job_id}
        self.launches.append(launch)
        if self.on_launch is not None:
            self.on_launch(launch)
        return job_id, f"https://hf.co/jobs/{job_id}"

    def watch_terminal(self, job_id: str) -> tuple[str, str | None, str]:
        return self.terminal

    def fetch_logs_text(self, job_id: str) -> str:
        return self.logs


# ── seeding helpers ──────────────────────────────────────────────────


def seed_agent(
    hub: FakeHub,
    agent_id: str,
    *,
    hf_user: str = "test-user",
    model: str = "opus-4.7",
    harness: str = "claude-code",
    joined: str = "2026-06-01 10:00 UTC",
    bio: str = "",
) -> str:
    fm = {
        "agent_name": agent_id,
        "agent_model": model,
        "agent_harness": harness,
        "agent_tools": ["bash"],
        "hf_user": hf_user,
        "agent_bucket": f"test-org/test-{agent_id}",
        "joined": joined,
    }
    hub.seed(f"agents/{agent_id}.md", serialise(fm, bio))
    return f"{agent_id}.md"


def seed_message(
    hub: FakeHub,
    stamp: str,
    agent: str,
    body: str,
    **fm,
) -> str:
    merged = {"agent": agent, "timestamp": "2026-06-01 10:00 UTC", "via": "raw", "type": "agent"}
    merged.update(fm)
    filename = f"{stamp}_{agent}.md"
    hub.seed(f"message_board/{filename}", serialise(merged, body))
    return filename


def seed_result(
    hub: FakeHub,
    stamp: str,
    agent: str,
    score: float,
    *,
    status: str = "agent-run",
    method: str = "vllm-baseline",
    description: str = "a result",
    body: str = "measured.",
    **fm,
) -> str:
    merged = {
        "score": score,
        "method": method,
        "status": status,
        "description": description,
        "agent": agent,
        "timestamp": "2026-06-01 10:00 UTC",
        "via": "bucket",
    }
    merged.update(fm)
    filename = f"{stamp}_{agent}.md"
    hub.seed(f"results/{filename}", serialise(merged, body))
    return filename
