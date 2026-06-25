"""OTLP/HTTP ingest routes: POST /v1/{traces,metrics,logs}.

The OTel SDK appends the signal path to OTEL_EXPORTER_OTLP_ENDPOINT, so agents
point at the Space root and these three routes catch each signal. Bodies are
accepted as-is (protobuf or JSON) and buffered by the telemetry sink; we never
decode them here. Callers authenticate with a single shared bearer token
(OTEL_INGEST_TOKEN); per-agent attribution comes from OTEL_SERVICE_NAME inside
the payload, matching the collaboration's existing identity model.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response

from app.auth import extract_bearer
from app.config import Settings
from app.deps import get_settings_dep, get_telemetry_sink
from app.errors import Unauthorized
from app.telemetry_sink import TelemetrySink


router = APIRouter()


def _check_token(authorization: str | None, settings: Settings) -> None:
    token = extract_bearer(authorization)
    if not settings.otel_ingest_token or token != settings.otel_ingest_token:
        raise Unauthorized(
            "invalid or missing OTLP ingest token",
            hint="set OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <OTEL_INGEST_TOKEN>",
        )


async def _ingest(
    signal: str,
    request: Request,
    authorization: str | None,
    settings: Settings,
    sink: TelemetrySink,
) -> Response:
    _check_token(authorization, settings)
    body = await request.body()
    content_type = request.headers.get("content-type", "application/x-protobuf")
    sink.append(signal, body, content_type)
    # Empty 200 is accepted as success by OTLP/HTTP exporters (protobuf or JSON).
    return Response(status_code=200)


@router.post("/v1/traces")
async def post_traces(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    sink: TelemetrySink = Depends(get_telemetry_sink),
) -> Response:
    return await _ingest("traces", request, authorization, settings, sink)


@router.post("/v1/metrics")
async def post_metrics(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    sink: TelemetrySink = Depends(get_telemetry_sink),
) -> Response:
    return await _ingest("metrics", request, authorization, settings, sink)


@router.post("/v1/logs")
async def post_logs(
    request: Request,
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
    sink: TelemetrySink = Depends(get_telemetry_sink),
) -> Response:
    return await _ingest("logs", request, authorization, settings, sink)
