"""
flask_weather_app.py — Browser UI for the Chennai weather telemetry data
=========================================================================

Serves a simple auto-refreshing web page that displays the most recent
weather/AQI snapshot produced by weather_collector.py, and emits all three
OTel signals to Grafana Cloud:

Routes:
    GET /                     — index page, shows latest telemetry snapshot
    GET /data/<filename>      — returns a specific telemetry JSON file as JSON

Traces → Grafana Tempo:
    FlaskInstrumentor()       — auto-creates a root span for every HTTP request
                                with http.method, http.route, http.status_code
    load_latest_telemetry     — manual child span in the / handler
    serve_telemetry_file      — manual child span in the /data/<filename> handler

Metrics → Grafana Cloud Metrics:
    http_requests_total          counter    — total requests per route+method
    http_request_duration_seconds histogram  — latency per route+method+status
                                              (exemplar-enabled: embeds trace_id)
    telemetry_files_loaded       histogram  — file count per index request
                                              (exemplar-enabled: embeds trace_id)
    telemetry_file_not_found_total counter  — 404 count per filename

Logs → Grafana Loki:
    All logger.*() calls are bridged to the OTel LoggerProvider via
    LoggingHandler. Every record is tagged with the active span's trace_id,
    so Grafana can correlate log lines with traces.

Required environment variables:
    OTEL_EXPORTER_OTLP_ENDPOINT  — Grafana OTLP gateway URL
    OTEL_EXPORTER_OTLP_HEADERS   — Grafana Cloud token
"""

import os
import json
import glob
import logging
import time   # perf_counter for high-resolution request duration measurement
from flask import Flask, render_template_string, jsonify, request

from opentelemetry import trace, metrics
from opentelemetry.instrumentation.flask import FlaskInstrumentor

# Shared OTel bootstrap — must be the first OTel call in the module.
from otel_config import init_otel


# ── OTel bootstrap ────────────────────────────────────────────────────────────
# init_otel() registers the global TracerProvider, MeterProvider, and
# LoggerProvider (all backed by OTLP/HTTP exporters pointed at Grafana Cloud).
# FlaskInstrumentor.instrument_app() below uses the global TracerProvider.
tracer, meter, _logger_provider = init_otel(
    service_name="flask-weather-app",
    metrics_interval_ms=15_000,   # push metrics to Grafana every 15 s
)

# Standard Python logger — records flow to both the console AND Grafana Loki
# because otel_config.attach_otel_logging_handler() added a LoggingHandler
# to the root logger during init_otel().
logger = logging.getLogger(__name__)


# ── Metric instruments ────────────────────────────────────────────────────────

# Simple counter: incremented once per request before entering any span.
# Used in Grafana to compute request rate (rate(http_requests_total[5m])).
request_counter = meter.create_counter(
    name="http_requests_total",
    description="Total HTTP requests received by the Flask app",
)

# Latency histogram: records end-to-end request duration in seconds.
# This is the primary exemplar-bearing instrument for the Flask app.
# Because .record() is called INSIDE the active FlaskInstrumentor span +
# our manual child span, the TraceBasedExemplarFilter embeds the trace_id
# and span_id. In Grafana this creates scatter dots on the p99 latency chart
# with two drilldown links:
#   "→ Tempo: view trace"         — waterfall of the slow request
#   "→ Loki: logs for this trace" — all log lines emitted during that request
request_duration_histogram = meter.create_histogram(
    name="http_request_duration_seconds",
    description="End-to-end HTTP request duration in seconds",
    unit="s",
)

# Records how many telemetry JSON files exist on disk per index request.
# Spikes here indicate the collector is running faster than files are pruned.
file_load_histogram = meter.create_histogram(
    name="telemetry_files_loaded",
    description="Number of telemetry files found per index request",
    unit="files",
)

# Counts requests for files that don't exist (404s).
# Useful for alerting if something upstream sends bad filenames.
file_not_found_counter = meter.create_counter(
    name="telemetry_file_not_found_total",
    description="Requests for a telemetry file that did not exist on disk",
)


# ── Flask application ─────────────────────────────────────────────────────────
app = Flask(__name__)

# FlaskInstrumentor wraps every route handler in an OTel span automatically.
# The span is named after the HTTP route (e.g. "GET /data/<filename>") and
# carries http.method, http.route, http.status_code, and http.url attributes.
# Our manual child spans (load_latest_telemetry, serve_telemetry_file) are
# nested inside this auto-created span, giving a two-level waterfall in Tempo.
FlaskInstrumentor().instrument_app(app)

# Directory where weather_collector.py writes its JSON snapshots.
DATA_DIR = "./telemetry_data"

# Simple Jinja2 HTML template — auto-refreshes every 10 s so the browser
# always shows the latest reading without a manual reload.
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>OpenWeatherMap Telemetry</title>
    <meta http-equiv="refresh" content="10">
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        pre  { background-color: #f4f4f4; padding: 10px; border-radius: 5px; overflow-x: auto; }
        h1   { color: #333; }
        p    { color: #555; }
    </style>
</head>
<body>
    <h1>Latest OpenWeatherMap Telemetry Data</h1>
    <p>Last updated: {{ timestamp }} (Page refreshes every 10 seconds)</p>
    <pre>{{ json_data }}</pre>
    <h2>All Collected Files:</h2>
    <ul>
        {% for file in files %}
            <li><a href="/data/{{ file }}">{{ file }}</a></li>
        {% endfor %}
    </ul>
</body>
</html>
"""


@app.route("/")
def index():
    """
    Index route — reads the most recent telemetry snapshot and renders the page.

    Observability emitted:
        - Span:    FlaskInstrumentor root span (auto) + load_latest_telemetry (manual)
        - Metrics: http_requests_total (+1), telemetry_files_loaded (file count),
                   http_request_duration_seconds (elapsed seconds with exemplar)
        - Logs:    "Serving latest telemetry file: ..." (tagged with trace_id)
    """
    # Start the wall-clock timer before any work so we capture the full duration.
    t0 = time.perf_counter()

    # Count this request before opening the span so the counter increment
    # is visible even if the span fails to open (defensive practice).
    request_counter.add(1, {"route": "/", "method": request.method})

    # Manual child span for the file-loading work.
    # This nests under the FlaskInstrumentor auto-span and gives Tempo a
    # clean separation between "HTTP framework overhead" and "application logic".
    with tracer.start_as_current_span("load_latest_telemetry") as span:
        # Glob all telemetry files and sort newest-first by creation time.
        json_files = sorted(
            glob.glob(os.path.join(DATA_DIR, "telemetry_*.json")),
            key=os.path.getctime,
            reverse=True,
        )
        file_count = len(json_files)

        # ── Exemplar recording ─────────────────────────────────────────────
        # Recorded inside the active span → TraceBasedExemplarFilter embeds
        # trace_id on this data point. Clicking the scatter dot in Grafana
        # opens the trace that served this index request.
        file_load_histogram.record(file_count, {"route": "/"})
        span.set_attribute("telemetry.file_count", file_count)

        latest_data = "No telemetry data found."
        timestamp   = "N/A"
        file_list   = []

        if json_files:
            latest_file = json_files[0]
            # Extract the timestamp portion from the filename, e.g.
            # "telemetry_20260930213304.json" → "20260930213304"
            timestamp = (
                os.path.basename(latest_file)
                .replace("telemetry_", "")
                .replace(".json", "")
            )
            span.set_attribute("telemetry.latest_file", os.path.basename(latest_file))

            try:
                with open(latest_file, "r") as f:
                    data = json.load(f)
                latest_data = json.dumps(data, indent=2)
                logger.info("Serving latest telemetry file: %s", latest_file)
                # ↑ Loki receives this with trace_id injected by the LoggingHandler.
            except OSError as exc:
                logger.error("Failed to read telemetry file %s: %s", latest_file, exc)
                # record_exception stores the traceback in the span as an event.
                span.record_exception(exc)
                latest_data = f"Error reading file: {exc}"

            file_list = [os.path.basename(f) for f in json_files]

        response = render_template_string(
            HTML_TEMPLATE,
            json_data=latest_data,
            timestamp=timestamp,
            files=file_list,
        )

        # ── Duration exemplar ─────────────────────────────────────────────
        # .record() is called while BOTH the FlaskInstrumentor span and our
        # load_latest_telemetry child span are on the context stack.
        # The exemplar will carry the child span's trace_id + span_id.
        duration = time.perf_counter() - t0
        request_duration_histogram.record(
            duration,
            {"route": "/", "method": request.method},
        )
        return response


@app.route("/data/<filename>")
def get_json_file(filename):
    """
    File route — returns a specific telemetry JSON file as JSON.

    Observability emitted:
        - Span:    FlaskInstrumentor root span (auto) + serve_telemetry_file (manual)
        - Metrics: http_requests_total (+1),
                   http_request_duration_seconds (with status label + exemplar),
                   telemetry_file_not_found_total (+1 on 404)
        - Logs:    file served / warning on 404 / error on read failure
    """
    t0 = time.perf_counter()
    request_counter.add(1, {"route": "/data/<filename>", "method": request.method})

    with tracer.start_as_current_span("serve_telemetry_file") as span:
        span.set_attribute("requested.filename", filename)

        # ── Path-traversal guard ──────────────────────────────────────────
        # os.path.basename strips any leading "../" components so a request
        # for "../../etc/passwd" cannot escape the DATA_DIR.
        safe_name = os.path.basename(filename)
        filepath  = os.path.join(DATA_DIR, safe_name)

        if not os.path.exists(filepath):
            file_not_found_counter.add(1, {"filename": safe_name})
            logger.warning("Requested file not found: %s", safe_name)
            span.set_attribute("file.found", False)
            duration = time.perf_counter() - t0
            # Include status label on the duration metric so Grafana can
            # plot 404 latency separately from 200 latency.
            request_duration_histogram.record(
                duration,
                {"route": "/data/<filename>", "method": request.method, "status": "404"},
            )
            return "File not found", 404

        try:
            with open(filepath, "r") as f:
                payload = json.load(f)
            logger.info("Serving telemetry file: %s", safe_name)
            span.set_attribute("file.found", True)
            duration = time.perf_counter() - t0
            # ── Duration exemplar ─────────────────────────────────────────
            # Recorded inside the serve_telemetry_file span → carries its
            # trace_id. The "status": "200" label lets Grafana filter
            # latency charts by success vs error responses.
            request_duration_histogram.record(
                duration,
                {"route": "/data/<filename>", "method": request.method, "status": "200"},
            )
            return jsonify(payload)

        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Error reading %s: %s", safe_name, exc)
            span.record_exception(exc)
            duration = time.perf_counter() - t0
            request_duration_histogram.record(
                duration,
                {"route": "/data/<filename>", "method": request.method, "status": "500"},
            )
            return f"Error reading file: {exc}", 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    logger.info("Starting Flask weather app on port 5000")
    # use_reloader=False: the reloader forks the process, which would call
    # init_otel() twice and register duplicate OTel providers.
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
