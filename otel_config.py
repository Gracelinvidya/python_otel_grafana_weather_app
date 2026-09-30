"""
otel_config.py — Shared OpenTelemetry bootstrap for Grafana Cloud export
========================================================================

This module is the single source of truth for all OpenTelemetry wiring.
Both weather_collector.py and flask_weather_app.py call init_otel() once
at startup and receive a ready-to-use tracer, meter, and logger_provider.

Signal pipeline (all three signals share the same auth and endpoint):

    Python code
        │
        ├─ trace.get_tracer()  →  TracerProvider
        │                              └─ BatchSpanProcessor
        │                                     └─ OTLPSpanExporter  ──► /v1/traces
        │
        ├─ metrics.get_meter() →  MeterProvider
        │                              └─ PeriodicExportingMetricReader (15 s)
        │                                     └─ OTLPMetricExporter ──► /v1/metrics
        │
        └─ logging.getLogger() → stdlib Logger
                                      └─ LoggingHandler (OTel bridge)
                                             └─ LoggerProvider
                                                    └─ BatchLogRecordProcessor
                                                           └─ OTLPLogExporter ──► /v1/logs

All three signals are sent to:
    https://otlp-gateway-prod-ap-south-1.grafana.net/otlp

Authentication uses HTTP Basic Auth derived from the env var
OTEL_EXPORTER_OTLP_HEADERS (see _parse_headers for format details).

Exemplar flow:
    TraceBasedExemplarFilter is attached to the MeterProvider.
    Whenever a histogram.record() or counter.add() is called while a
    sampled span is active, the SDK automatically embeds the current
    trace_id and span_id into that metric data point as an exemplar.
    Grafana reads these exemplars and renders clickable scatter dots on
    time-series charts that deep-link into Tempo.

Environment variables required:
    OTEL_EXPORTER_OTLP_ENDPOINT  – e.g. https://otlp-gateway-prod-ap-south-1.grafana.net/otlp
    OTEL_EXPORTER_OTLP_HEADERS   – Grafana Cloud token (bare base64 or "Authorization=Basic ...")
"""

import os
import logging

# python-dotenv loads key=value pairs from a .env file into os.environ.
# load_dotenv() is a no-op if the file doesn't exist (safe for production
# environments where secrets are injected by the platform instead).
# override=False means shell-level env vars always win over .env values,
# so CI/CD pipelines can still inject secrets without editing .env.
from dotenv import load_dotenv
load_dotenv(override=False)

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

# ── Trace SDK ────────────────────────────────────────────────────────────────
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
# OTLPSpanExporter sends spans as protobuf over HTTP to the /v1/traces endpoint.
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# ── Metrics SDK ──────────────────────────────────────────────────────────────
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
# OTLPMetricExporter sends metric batches as protobuf over HTTP to /v1/metrics.
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
# TraceBasedExemplarFilter: only attaches exemplars when a sampled span is
# active, avoiding exemplar noise on metrics recorded outside of traces.
from opentelemetry.sdk.metrics._internal.exemplar import TraceBasedExemplarFilter

# ── Logs SDK ─────────────────────────────────────────────────────────────────
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
# OTLPLogExporter sends log records as protobuf over HTTP to /v1/logs.
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter


# ── Connection details ────────────────────────────────────────────────────────
# Read from environment so credentials are never hard-coded in source.
# Values come from .env (loaded above) or from shell environment variables.
# Raises a clear error at startup if the required endpoint is missing,
# rather than silently sending to a wrong/default URL.
OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if not OTLP_ENDPOINT:
    raise EnvironmentError(
        "OTEL_EXPORTER_OTLP_ENDPOINT is not set. "
        "Copy .env.example to .env and fill in your Grafana Cloud OTLP endpoint."
    )

OTLP_HEADERS_RAW = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
if not OTLP_HEADERS_RAW:
    raise EnvironmentError(
        "OTEL_EXPORTER_OTLP_HEADERS is not set. "
        "Copy .env.example to .env and fill in your Grafana Cloud token."
    )


def _parse_headers(raw: str) -> dict:
    """
    Parse OTEL_EXPORTER_OTLP_HEADERS into the dict expected by OTLP exporters.

    Grafana Cloud supplies this env var in one of two formats depending on
    which page you copied it from:

    Format 1 — Standard OTel multi-header string (key=value pairs):
        "Authorization=Basic MTgw..."
        "key1=val1,key2=val2"
        Detected by: the text before the first '=' is a short valid header name
        (< 50 chars, only letters/digits/hyphens).

    Format 2 — Bare base64 token (Grafana's default OTLP snippet):
        "MTgwODcwNTpnbGN..."
        The decoded value is "instanceID:token" (HTTP Basic Auth credentials).
        Detected by: the text before '=' is too long or contains non-header chars.
        Action: wrap it as {"Authorization": "Basic <token>"}

    Returns a dict like {"Authorization": "Basic MTgw..."} ready to be passed
    directly to OTLPSpanExporter / OTLPMetricExporter / OTLPLogExporter.
    """
    if not raw:
        return {}

    raw = raw.strip()

    # Try to detect Format 1 by inspecting the candidate key name.
    # Base64 strings are 100+ chars; a real header name is short (e.g. "Authorization" = 13 chars).
    first_eq = raw.find("=")
    if first_eq > 0:
        candidate_key = raw[:first_eq]
        import re
        if len(candidate_key) < 50 and re.match(r'^[A-Za-z][A-Za-z0-9\-]*$', candidate_key):
            # Format 1: parse comma-separated key=value pairs
            headers = {}
            for pair in raw.split(","):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()
            return headers

    # Format 2: bare base64 token — the decoded string is "instanceID:apiToken"
    # which is exactly the credential pair for HTTP Basic Auth.
    return {"Authorization": f"Basic {raw}"}


# Build the header dict once at import time; all exporters share it.
OTLP_HEADERS = _parse_headers(OTLP_HEADERS_RAW)


# ── Resource ─────────────────────────────────────────────────────────────────

def make_resource(service_name: str) -> Resource:
    """
    Create an OTel Resource tagged with the service name.

    The Resource is attached to every span, metric data point, and log record
    so Grafana can filter by service (e.g. service_name="flask-weather-app").
    Additional attributes (host.name, process.pid, etc.) are auto-detected by
    Resource.create() from the runtime environment.
    """
    return Resource.create({SERVICE_NAME: service_name})


# ── Provider factories ────────────────────────────────────────────────────────

def init_tracer_provider(resource: Resource) -> TracerProvider:
    """
    Build and globally register a TracerProvider that exports to Grafana Tempo.

    BatchSpanProcessor buffers finished spans in memory and flushes them in
    batches (default: max 512 spans or 5 s, whichever comes first), reducing
    the number of HTTP round-trips to the OTLP gateway.

    After this call, any code that does:
        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span("my-span"):
            ...
    will automatically export those spans to Grafana Tempo.
    """
    exporter = OTLPSpanExporter(
        endpoint=f"{OTLP_ENDPOINT}/v1/traces",
        headers=OTLP_HEADERS,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    # Register globally so trace.get_tracer() anywhere in the process uses this provider.
    trace.set_tracer_provider(provider)
    return provider


def init_meter_provider(resource: Resource, export_interval_ms: int = 15_000) -> MeterProvider:
    """
    Build and globally register a MeterProvider that exports to Grafana Mimir/Prometheus.

    Key design decisions:
    - PeriodicExportingMetricReader: pushes metrics every `export_interval_ms`
      milliseconds (default 15 s). Grafana Cloud ingests these as a Prometheus
      remote-write compatible time series.
    - TraceBasedExemplarFilter: when a histogram.record() or counter.add() call
      is made inside an active sampled span, the SDK attaches that span's
      trace_id and span_id to the metric data point as an "exemplar". Grafana
      renders these as scatter dots on metric charts — clicking one opens the
      linked trace in Tempo.

    After this call, any code that does:
        meter = metrics.get_meter(__name__)
        counter = meter.create_counter("my.counter")
        counter.add(1)
    will automatically export to Grafana Cloud Metrics.
    """
    exporter = OTLPMetricExporter(
        endpoint=f"{OTLP_ENDPOINT}/v1/metrics",
        headers=OTLP_HEADERS,
    )
    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=export_interval_ms,
    )
    provider = MeterProvider(
        resource=resource,
        metric_readers=[reader],
        # Exemplar filter: only embed trace context in metric data points when
        # a sampled span is currently active in the same thread/async context.
        exemplar_filter=TraceBasedExemplarFilter(),
    )
    # Register globally so metrics.get_meter() anywhere in the process uses this provider.
    metrics.set_meter_provider(provider)
    return provider


def init_logger_provider(resource: Resource) -> LoggerProvider:
    """
    Build and globally register a LoggerProvider that exports to Grafana Loki.

    BatchLogRecordProcessor works like BatchSpanProcessor — it buffers OTel
    log records and flushes them in batches to the OTLP /v1/logs endpoint.

    Note: this provider is wired to Python's stdlib logging in
    attach_otel_logging_handler() below. Any logger.info() / logger.error()
    call then flows through both the standard console handler AND this OTel
    pipeline to Grafana Loki.

    The OTel SDK automatically injects the active span's trace_id and span_id
    into every log record, enabling trace-to-log correlation in Grafana.
    """
    exporter = OTLPLogExporter(
        endpoint=f"{OTLP_ENDPOINT}/v1/logs",
        headers=OTLP_HEADERS,
    )
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    # Register globally so the LoggingHandler bridge can find this provider.
    set_logger_provider(provider)
    return provider


def attach_otel_logging_handler(logger_provider: LoggerProvider, level: int = logging.DEBUG):
    """
    Bridge Python's stdlib logging module into the OTel LoggerProvider.

    Without this, logging.getLogger(__name__).info("...") only goes to the
    console. After this call, every log record at or above `level` is also
    converted to an OTel LogRecord and exported to Grafana Loki via the
    provider's BatchLogRecordProcessor.

    The bridge preserves the log level, message, and any extra fields.
    The OTel SDK additionally injects:
        - trace_id  (hex string of the active span's trace ID)
        - span_id   (hex string of the active span's span ID)
        - service.name  (from the Resource)
    These fields are what Grafana uses to correlate logs with traces.
    """
    handler = LoggingHandler(level=level, logger_provider=logger_provider)
    # Attach to the root logger so ALL loggers in the process are captured.
    logging.getLogger().addHandler(handler)
    return handler


# ── Public entry point ────────────────────────────────────────────────────────

def init_otel(service_name: str, metrics_interval_ms: int = 15_000):
    """
    Initialise all three OTel signal pipelines in one call.

    Call this ONCE at the very top of your application, before any spans,
    metrics, or log statements are created.

    Parameters
    ----------
    service_name : str
        Identifies this service in Grafana. Used as the `service.name` Resource
        attribute and as the Loki label `service_name`.
    metrics_interval_ms : int
        How often (in milliseconds) the metric reader pushes data to Grafana.
        Lower values = more up-to-date dashboards, more network traffic.
        Default: 15 000 ms (15 seconds).

    Returns
    -------
    (tracer, meter, logger_provider) : tuple
        - tracer         : opentelemetry.trace.Tracer    — use for start_as_current_span()
        - meter          : opentelemetry.metrics.Meter   — use for create_counter/histogram
        - logger_provider: LoggerProvider                — usually kept as _logger_provider
                           (you use stdlib logging directly; this is for shutdown hooks)
    """
    resource = make_resource(service_name)

    # Order matters: TracerProvider first so exemplar filter can read span context.
    tracer_provider = init_tracer_provider(resource)
    meter_provider  = init_meter_provider(resource, export_interval_ms=metrics_interval_ms)
    logger_provider = init_logger_provider(resource)

    # Wire stdlib logging → OTel logs pipeline
    attach_otel_logging_handler(logger_provider)

    return (
        trace.get_tracer(service_name),
        metrics.get_meter(service_name),
        logger_provider,
    )
