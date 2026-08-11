"""Optional OpenTelemetry -> Google Cloud Trace configuration."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

_configured = False
_tracer: Any = None
_last_error: str | None = None


def configure_telemetry() -> dict[str, Any]:
    global _configured, _tracer, _last_error
    if _configured:
        return telemetry_status()
    enabled = os.getenv("SHIFTZERO_CLOUD_TRACE_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
    }
    if not enabled:
        return telemetry_status()
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": os.getenv("K_SERVICE", "shiftzero-api"),
                    "service.version": os.getenv("K_REVISION", "local"),
                    "cloud.provider": "gcp",
                }
            )
        )
        provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("shiftzero.operations", "0.2.0")
        _configured = True
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: {exc}"[:500]
    return telemetry_status()


def telemetry_status() -> dict[str, Any]:
    return {
        "provider": "google-cloud-trace" if _configured else "local-trace-ids",
        "configured": _configured,
        "last_error": _last_error,
    }


@contextmanager
def trace_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name, attributes=attributes or {}):
        yield
