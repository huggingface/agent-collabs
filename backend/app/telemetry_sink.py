"""In-memory OTLP buffer that flushes batches to the traces bucket.

Each incoming OTLP/HTTP request body (protobuf or JSON, for any of the three
signals) is wrapped in a one-line base64 envelope and buffered. A background
task drains every signal's buffer on a timer (or sooner when it grows past a
size threshold) and writes the batch as a single new object in the traces
bucket. Archiving the raw body is lossless and trivially replayable later
(decode each line, replay with its recorded content-type) and needs no
``opentelemetry-proto`` dependency.

The Space is the sole writer to the traces bucket, so every flush is a fresh,
uniquely-named file — never an append, never two writers on one path (contrast
``HubClient._append_jsonl``, which re-uploads the whole file per line and must
not be used for the telemetry firehose).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import anyio

from app.config import Settings
from app.hub import HubClient
from app.naming import stamp_iso, telemetry_path, utc_now


log = logging.getLogger(__name__)

SIGNALS: tuple[str, ...] = ("traces", "metrics", "logs")


class TelemetrySink:
    def __init__(self, hub: HubClient, settings: Settings):
        self._hub = hub
        self._settings = settings
        self._bucket = settings.traces_bucket
        self._buffers: dict[str, list[bytes]] = {s: [] for s in SIGNALS}
        self._sizes: dict[str, int] = {s: 0 for s in SIGNALS}
        self._seq = 0
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ───────────────────────── ingest side (request handlers) ─────────────────────────

    def append(self, signal: str, body: bytes, content_type: str) -> None:
        """Buffer one OTLP request body. Synchronous on purpose: called from an
        async handler with no intervening await, so it is atomic under asyncio."""
        if signal not in self._buffers:
            raise ValueError(f"unknown signal: {signal}")
        envelope = {
            "ts": stamp_iso(utc_now()),
            "content_type": content_type,
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        line = json.dumps(envelope, separators=(",", ":")).encode("utf-8") + b"\n"
        self._buffers[signal].append(line)
        self._sizes[signal] += len(line)
        if self._sizes[signal] >= self._settings.otel_flush_max_bytes:
            self._wake.set()

    # ───────────────────────── lifecycle ─────────────────────────

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="otel-flusher")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self._flush_all()  # final drain

    # ───────────────────────── flush side (background task) ─────────────────────────

    async def _run(self) -> None:
        interval = self._settings.otel_flush_seconds
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self._flush_all()

    async def _flush_all(self) -> None:
        for signal in SIGNALS:
            await self._flush(signal)

    async def _flush(self, signal: str) -> None:
        if not self._buffers[signal]:
            return
        # Swap the buffer out synchronously (no await) so concurrent appends
        # land in the fresh list rather than the batch being written.
        lines = self._buffers[signal]
        self._buffers[signal] = []
        self._sizes[signal] = 0
        self._seq += 1
        path = telemetry_path(signal, utc_now(), self._seq)
        data = b"".join(lines)
        try:
            await anyio.to_thread.run_sync(
                self._hub.write_bytes_to_bucket, self._bucket, path, data
            )
            log.info(
                "otel flush: %s -> %s/%s (%d records, %d bytes)",
                signal, self._bucket, path, len(lines), len(data),
            )
        except Exception:
            # Best-effort telemetry: drop the batch rather than grow unbounded
            # if the bucket is unwritable. This is the accepted loss window.
            log.exception(
                "otel flush failed for %s; dropped %d records (%d bytes)",
                signal, len(lines), len(data),
            )
