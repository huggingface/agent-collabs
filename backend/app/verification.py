"""Verification-status index for promoted results.

``results/verification_status.json`` is a flat map of result filename ->
verification state (``pending`` | ``valid`` | ``invalid``). A human (or a
downstream verifier) flips entries to ``valid`` / ``invalid``; every newly
promoted result is inserted as ``pending`` so nothing slips through unreviewed.

Maintaining it is a read-modify-write of a single shared JSON object, so a
process-wide lock serialises the update — the Space runs a single uvicorn worker
and sync endpoints share a threadpool, the same assumption ``DurableJobQuota``
relies on. The update is **best-effort and non-fatal**: by the time we get here
the result file is already promoted, so a storage hiccup must not fail the
request — we log and move on, and the next promotion (or a manual reconcile)
heals the index.

Two safety properties:

- **Verdicts are never clobbered.** A filename already present (whatever its
  state) is left untouched — we only ever *insert* a missing entry as
  ``pending``, never overwrite an existing ``valid`` / ``invalid``.
- **Fail SAFE on read.** If the index can't be read (transport error) or parsed
  (corrupt/non-object JSON), we refuse to write — overwriting would erase every
  human verdict. We skip and log loudly so it can be fixed by hand.

``set_verdict`` (the automated verifier's write path, §5.7) extends the first
property with a **side-ledger compare-and-set**: every verdict the verifier
writes is recorded in the private audit bucket
(``verification_runs/<filename>/verdict.json``), and the index is only updated
when the current entry is ``pending``/absent **or** equals the verifier's own
last-written value. Any other ``valid``/``invalid`` must have been set (or
changed) by a human, and the human verdict wins — even over the verifier's own
earlier one. The index itself stays a flat ``{filename: state}`` map; verdict
provenance lives only in the private ledger.
"""
from __future__ import annotations

import json
import logging
import threading

from app.hub import HubClient
from app.naming import VERIFICATION_STATUS_PATH, stamp_iso, utc_now


log = logging.getLogger(__name__)

PENDING = "pending"
VALID = "valid"
INVALID = "invalid"

# set_verdict outcomes.
WRITTEN = "written"      # index updated, ledger record stored
DEFERRED = "deferred"    # a human verdict is in place; left untouched
SKIPPED = "skipped"      # index unreadable/corrupt — fail-safe, no write


class VerificationStatusStore:
    def __init__(
        self,
        hub: HubClient,
        path: str = VERIFICATION_STATUS_PATH,
        runs_prefix: str = "verification_runs",
    ):
        self._hub = hub
        self._path = path
        self._runs_prefix = runs_prefix.strip("/")
        self._lock = threading.Lock()

    def _load(self) -> dict | None:
        """Current index, or ``None`` if it cannot be safely read/parsed.

        ``None`` means "do not write" — distinct from ``{}``, a genuinely absent
        index that is safe to create from scratch.
        """
        try:
            raw = self._hub.read_central_bytes_optional(self._path)
        except Exception as exc:
            log.warning("verification-status read failed for %s: %s", self._path, exc)
            return None
        if raw is None:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.error(
                "verification-status index at %s is unparseable (%s); refusing to "
                "overwrite", self._path, exc,
            )
            return None
        if not isinstance(data, dict):
            log.error(
                "verification-status index at %s is not a JSON object; refusing to "
                "overwrite", self._path,
            )
            return None
        return data

    def mark_pending(self, filename: str) -> None:
        """Insert ``filename`` as ``pending`` if absent. Best-effort; never raises.

        Serialised by a process-wide lock so two concurrent promotions cannot
        read-then-write the same index and drop each other's entry. Existing
        entries (including human ``valid`` / ``invalid`` verdicts) are preserved.
        """
        with self._lock:
            data = self._load()
            if data is None:
                return  # read/parse failed — fail safe, leave the index untouched
            if filename in data:
                return  # already tracked; don't rewrite or clobber a verdict
            data[filename] = PENDING
            body = json.dumps(data, indent=2, sort_keys=True) + "\n"
            try:
                self._hub.write_text_central(self._path, body)
            except Exception as exc:
                log.warning(
                    "verification-status write failed for %s: %s", self._path, exc
                )

    # ───────────────────── automated verdicts (§5.7) ─────────────────────

    def _ledger_path(self, filename: str) -> str:
        return f"{self._runs_prefix}/{filename}/verdict.json"

    def ledger_state(self, filename: str) -> str | None:
        """The last state THIS verifier wrote for ``filename``, or None.

        Read from the private audit bucket. A transport error degrades to None
        — the CAS then treats a non-pending index entry as human-authored and
        defers, which is the safe direction.
        """
        try:
            raw = self._hub.read_audit_bytes(self._ledger_path(filename))
        except Exception as exc:
            log.warning("verdict-ledger read failed for %s: %s", filename, exc)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("verdict-ledger record for %s unparseable: %s", filename, exc)
            return None
        if not isinstance(data, dict):
            return None
        state = data.get("state")
        return str(state) if state is not None else None

    def set_verdict(
        self,
        filename: str,
        new_state: str,
        *,
        by: str,
        details: dict | None = None,
    ) -> str:
        """Record an automated verdict, never overwriting a human one.

        Compare-and-set against the side-ledger: write the index iff the
        current entry is ``pending``/absent or equals the verifier's own
        last-written state. Returns WRITTEN, DEFERRED, or SKIPPED. Serialised
        by the same process-wide lock as ``mark_pending``.
        """
        if new_state not in (VALID, INVALID):
            raise ValueError(f"verdict must be '{VALID}' or '{INVALID}', got {new_state!r}")
        with self._lock:
            data = self._load()
            if data is None:
                log.error(
                    "set_verdict(%s, %s): index unreadable; refusing to write "
                    "(fail-safe)", filename, new_state,
                )
                return SKIPPED
            cur = data.get(filename)
            if cur not in (None, PENDING) and cur != self.ledger_state(filename):
                log.info(
                    "deferring to human verdict for %s: index=%s, not "
                    "verifier-authored", filename, cur,
                )
                return DEFERRED
            data[filename] = new_state
            body = json.dumps(data, indent=2, sort_keys=True) + "\n"
            self._hub.write_text_central(self._path, body)
            record = {
                "filename": filename,
                "state": new_state,
                "by": by,
                "at": stamp_iso(utc_now()),
                **(details or {}),
            }
            try:
                self._hub.write_bytes_audit(
                    self._ledger_path(filename),
                    (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                )
            except Exception:
                # Index already updated; a missing ledger record only makes a
                # FUTURE re-verify defer to the (now verifier-authored) entry —
                # conservative, never destructive.
                log.exception("verdict-ledger write failed for %s", filename)
            return WRITTEN
