"""
test_full_otel.py — End-to-end OTel test

Sends one real trace span, one metric data point, and one log record to
Grafana Cloud and waits for the exporters to flush.

Reads all credentials from .env (no secrets hardcoded here).

Usage:
    python test_full_otel.py

After running, check Grafana Explore for service_name="otel-test-service":
    Tempo   → service.name = otel-test-service
    Metrics → test_counter_total
    Loki    → {service_name="otel-test-service"}
"""

import logging
import time
import os
from dotenv import load_dotenv

# Load .env before anything else so all env vars are available
# when otel_config.py is imported below.
load_dotenv(override=False)

# Verify required env vars are present before going further.
for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_HEADERS"):
    if not os.environ.get(var):
        raise SystemExit(
            f"ERROR: {var} is not set.\n"
            "Copy .env.example → .env and fill in your Grafana Cloud credentials."
        )

# Enable OTel SDK internal debug logging so we can see what the exporters do.
logging.basicConfig(level=logging.DEBUG)
for noisy in ("urllib3", "requests", "opentelemetry.instrumentation"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from otel_config import init_otel, OTLP_HEADERS, OTLP_ENDPOINT
from opentelemetry import trace, metrics

# Show connection info (no secrets — only the header key names and endpoint).
print(f"\n{'='*60}")
print(f"Endpoint  : {OTLP_ENDPOINT}")
print(f"Auth keys : {list(OTLP_HEADERS.keys())}")
auth_val = OTLP_HEADERS.get("Authorization", "")
# Print only the scheme + first 10 chars of the token to confirm it's set.
print(f"Auth value: {auth_val[:16]}...  (truncated for security)")
print(f"{'='*60}\n")

# Initialise all three OTel pipelines for the test service.
tracer, meter, _lp = init_otel("otel-test-service")
logger = logging.getLogger("otel-test")

# ── Emit a trace span ────────────────────────────────────────
tracer = trace.get_tracer("otel-test")
with tracer.start_as_current_span("test-span") as span:
    span.set_attribute("test.key", "hello-grafana")
    logger.info("Test log from test_full_otel.py")
    print("Span emitted, waiting for BatchSpanProcessor to flush...")

# ── Emit a metric data point ─────────────────────────────────
meter   = metrics.get_meter("otel-test")
counter = meter.create_counter("test_counter", description="End-to-end test counter")
counter.add(1, {"env": "test"})
print("Metric recorded.")

# Wait long enough for BatchSpanProcessor (default max-delay 5 s) and
# PeriodicExportingMetricReader (15 s interval) to flush.
print("Waiting 20 s for all exporters to flush to Grafana...")
time.sleep(20)

print("\nDone. Check Grafana now:")
print("  Tempo   → service.name = otel-test-service  (look for 'test-span')")
print("  Metrics → test_counter_total")
print('  Loki    → {service_name="otel-test-service"}')
