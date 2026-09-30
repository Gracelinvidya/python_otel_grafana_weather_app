"""
weather_collector.py — Chennai Weather & AQI data collector
============================================================

Fetches current weather (temperature, humidity) and Air Quality Index (AQI)
for Chennai from the OpenWeatherMap API every 10 seconds, and emits all three
OTel signals to Grafana Cloud:

    Traces  → Grafana Tempo
        Root span  : fetch_weather_data   (one per collection cycle)
        Child span : fetch_weather         (OpenWeatherMap /weather call)
        Child span : fetch_aqi             (OpenWeatherMap /air_pollution call)
        RequestsInstrumentor also creates automatic spans for each outbound
        HTTP call with URL, method, and status code attributes.

    Metrics → Grafana Cloud Metrics (Prometheus / Mimir)
        outdoor_temperature_celsius  histogram  — recorded inside fetch_weather span
        outdoor_humidity_percent     histogram  — recorded inside fetch_weather span
        air_quality_index            histogram  — recorded inside fetch_aqi span
        weather_fetch_total          counter    — incremented per source in finally block
        weather_fetch_errors_total   counter    — incremented only on exceptions

        All histogram .record() calls happen INSIDE an active sampled span, so
        TraceBasedExemplarFilter embeds the trace_id + span_id as an exemplar
        on each data point. Grafana renders these as clickable scatter dots
        linking a metric spike directly to the offending trace.

    Logs    → Grafana Loki
        Every logger.info/error() call is bridged to the OTel LoggerProvider
        via LoggingHandler (set up in otel_config.attach_otel_logging_handler).
        The SDK automatically injects trace_id and span_id into each log record,
        enabling log-to-trace correlation inside Grafana.

Local persistence:
    Each collection cycle writes a JSON snapshot to ./telemetry_data/
    This is consumed by flask_weather_app.py for the browser UI.

Required environment variables (set before running):
    OTEL_EXPORTER_OTLP_ENDPOINT  — Grafana OTLP gateway URL
    OTEL_EXPORTER_OTLP_HEADERS   — Grafana Cloud token (bare base64 or key=value)
"""

import requests        # HTTP client for OpenWeatherMap API calls
import time            # Used by the scheduler sleep loop
import json            # JSON serialisation for telemetry snapshot files
import os              # File-system paths and directory creation
import logging         # Python stdlib logging (bridged to OTel via LoggingHandler)
import schedule        # Lightweight cron-style scheduler
import threading       # Background thread for the schedule runner
from datetime import datetime, timezone  # UTC timestamps in ISO-8601 format
from time import perf_counter            # High-resolution timer for durations

from opentelemetry import trace
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.trace import Status, StatusCode

# otel_config calls load_dotenv() at import time, so all env vars
# (including OPENWEATHERMAP_API_KEY) are available after this import.
from otel_config import init_otel


# ── OTel bootstrap ────────────────────────────────────────────────────────────
# init_otel() must be called before any spans/metrics/logs are created.
# It returns a ready-to-use tracer and meter, both backed by the globally
# registered providers configured in otel_config.py.
tracer, meter, _logger_provider = init_otel(
    service_name="weather-collector-chennai",
    metrics_interval_ms=15_000,   # push metrics to Grafana every 15 s
)

# Auto-instrument the `requests` library.
# After this call, every requests.get() / requests.post() automatically
# creates a child span with http.url, http.method, http.status_code attributes.
# These child spans are nested under whatever span is currently active.
RequestsInstrumentor().instrument()

# Configure console logging format. The OTel LoggingHandler (added by
# attach_otel_logging_handler) is already attached to the root logger,
# so basicConfig just sets the console output format here.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Metric instruments ────────────────────────────────────────────────────────
# We use histograms (not gauges) for sensor readings because histograms
# support exemplars. A gauge's .set() call does not trigger the exemplar
# filter, but a histogram's .record() does. This means every reading emitted
# while inside a sampled span gets the trace_id/span_id attached, letting
# Grafana draw the "→ Tempo" and "→ Loki" drilldown links on metric charts.

temperature_histogram = meter.create_histogram(
    name="outdoor_temperature_celsius",
    description="Current outdoor temperature in Celsius",
    unit="Cel",
)
humidity_histogram = meter.create_histogram(
    name="outdoor_humidity_percent",
    description="Current outdoor humidity as a percentage",
    unit="%",
)
aqi_histogram = meter.create_histogram(
    name="air_quality_index",
    description="Air Quality Index (1=Good, 5=Very Poor) from OpenWeatherMap",
    unit="AQI",
)
# fetch_counter counts successful + failed attempts per source ("weather"/"aqi").
# Incremented in the finally block so it always fires regardless of success/failure.
fetch_counter = meter.create_counter(
    name="weather_fetch_total",
    description="Total number of weather/AQI fetch attempts",
)
# error_counter is only incremented in the except block so the error ratio
# (error_counter / fetch_counter) gives a clean 0–1 signal in Grafana.
error_counter = meter.create_counter(
    name="weather_fetch_errors_total",
    description="Total number of failed weather/AQI fetch attempts",
)


# ── API configuration ─────────────────────────────────────────────────────────
# OPENWEATHERMAP_API_KEY is loaded from .env by load_dotenv() in otel_config.py.
# Fail fast at startup rather than making API calls with a missing key.
OPENWEATHERMAP_API_KEY = os.environ.get("OPENWEATHERMAP_API_KEY")
if not OPENWEATHERMAP_API_KEY:
    raise EnvironmentError(
        "OPENWEATHERMAP_API_KEY is not set. "
        "Copy .env.example to .env and add your OpenWeatherMap API key."
    )
CHENNAI_LAT = 13.0827   # Chennai, Tamil Nadu, India
CHENNAI_LON = 80.2707

# Build the full API URLs once at import time to avoid repeated string formatting.
WEATHER_API_URL = (
    f"https://api.openweathermap.org/data/2.5/weather"
    f"?lat={CHENNAI_LAT}&lon={CHENNAI_LON}"
    f"&appid={OPENWEATHERMAP_API_KEY}&units=metric"   # units=metric → Celsius
)
AIR_POLLUTION_API_URL = (
    f"https://api.openweathermap.org/data/2.5/air_pollution"
    f"?lat={CHENNAI_LAT}&lon={CHENNAI_LON}"
    f"&appid={OPENWEATHERMAP_API_KEY}"
)

# Directory for JSON snapshot files consumed by the Flask UI.
DATA_DIR = "./telemetry_data"
os.makedirs(DATA_DIR, exist_ok=True)

# Shared attribute dict for all metric instruments — keeps labels consistent.
CITY_ATTRS = {"city": "Chennai"}


# ── Main collection function ──────────────────────────────────────────────────

def fetch_weather_and_aqi():
    """
    One complete data collection cycle:
        1. Open a root trace span  (fetch_weather_data)
        2. Fetch weather data      (child span: fetch_weather)
           → record temperature + humidity histograms (with exemplars)
        3. Fetch AQI data          (child span: fetch_aqi)
           → record AQI histogram (with exemplar)
        4. Log results             (bridged to Grafana Loki with trace_id)
        5. Save JSON snapshot to ./telemetry_data/

    The three signals are correlated via the shared trace_id:
        - The root span's trace_id appears in every log record emitted
          during this function (OTel LoggingHandler injects it).
        - The histogram exemplars embed the child span's trace_id so
          clicking a data point in a Grafana metric chart opens the
          exact span that produced that reading.
    """
    # Initialise the snapshot dict. trace_id and span_id will be filled in
    # once the root span is started below.
    telemetry_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "city": "Chennai",
        "temperature_celsius": None,
        "humidity_percent": None,
        "air_quality_index": None,
        "trace_id": None,   # hex trace ID — also present in Loki log records
        "span_id": None,    # hex span ID of the root span
        "log_messages": [], # human-readable log lines collected during this cycle
    }

    # ── Root span: fetch_weather_data ─────────────────────────────────────────
    # This span wraps the entire collection cycle. Its trace_id is the one
    # that links all three signals together in Grafana.
    with tracer.start_as_current_span("fetch_weather_data") as span:
        # Capture the trace context to embed in the JSON snapshot so it can
        # be used later when querying logs/traces from the UI.
        span_ctx = span.get_span_context()
        if span_ctx.is_valid:
            telemetry_record["trace_id"] = format(span_ctx.trace_id, "032x")
            telemetry_record["span_id"]  = format(span_ctx.span_id, "016x")

        # Span attributes are indexed by Tempo and appear in the trace waterfall.
        span.set_attributes({
            "city": "Chennai",
            "lat": CHENNAI_LAT,
            "lon": CHENNAI_LON,
        })
        # Span events are timestamped milestones within a span — visible in
        # the Tempo waterfall as vertical markers.
        span.add_event("starting_data_fetch")
        logger.info("Starting weather+AQI fetch for Chennai")
        # ↑ This log record is automatically tagged with the root span's
        #   trace_id by the OTel LoggingHandler, linking it to Tempo.

        # ── Child span: fetch_weather ─────────────────────────────────────────
        # A child span is used here (not a separate root span) so that the
        # weather HTTP call and the AQI HTTP call both appear as siblings
        # under the same parent trace. This gives a clean waterfall view in Tempo.
        with tracer.start_as_current_span("fetch_weather") as weather_span:
            t0 = perf_counter()
            try:
                # RequestsInstrumentor wraps this call and creates a grandchild
                # span automatically with http.url, http.status_code, etc.
                resp = requests.get(WEATHER_API_URL, timeout=10)
                resp.raise_for_status()   # raise HTTPError on 4xx/5xx
                data = resp.json()

                temperature = data["main"]["temp"]
                humidity    = data["main"]["humidity"]

                # ── Exemplar recording ──────────────────────────────────────
                # .record() is called while the fetch_weather span is active.
                # TraceBasedExemplarFilter (set up in init_meter_provider) sees
                # the active span is sampled and embeds its trace_id + span_id
                # into this histogram data point as an exemplar.
                # In Grafana, this creates a scatter dot on the
                # outdoor_temperature_celsius chart. Clicking it shows two links:
                #   "→ Tempo: view trace"         — opens the fetch_weather span waterfall
                #   "→ Loki: logs for this trace" — opens Loki filtered by this trace_id
                temperature_histogram.record(temperature, CITY_ATTRS)
                humidity_histogram.record(humidity, CITY_ATTRS)

                # Set structured attributes on the span for Tempo search/filtering.
                weather_span.set_attributes({
                    "weather.temperature_celsius": temperature,
                    "weather.humidity_percent": humidity,
                    "http.status_code": resp.status_code,
                })
                weather_span.set_status(Status(StatusCode.OK))

                telemetry_record["temperature_celsius"] = temperature
                telemetry_record["humidity_percent"]    = humidity

                msg = f"Weather: {temperature}°C, Humidity: {humidity}%"
                logger.info(msg)
                # ↑ This log is tagged with the fetch_weather span's trace_id
                #   so it appears in Loki alongside the weather sub-span in Tempo.
                telemetry_record["log_messages"].append(msg)

            except requests.exceptions.RequestException as exc:
                # Increment the error counter with source label for Grafana alerting.
                error_counter.add(1, {**CITY_ATTRS, "source": "weather"})
                msg = f"Weather fetch error: {exc}"
                logger.error(msg, exc_info=True)
                # record_exception stores the full stack trace in the span.
                # In Tempo it appears as a span event with type "exception".
                weather_span.record_exception(exc)
                weather_span.set_status(Status(StatusCode.ERROR, str(exc)))
                telemetry_record["log_messages"].append(msg)
            finally:
                # Always count this attempt regardless of success or failure.
                # The error ratio in Grafana is: error_counter / fetch_counter.
                fetch_counter.add(1, {**CITY_ATTRS, "source": "weather"})

        # ── Child span: fetch_aqi ─────────────────────────────────────────────
        with tracer.start_as_current_span("fetch_aqi") as aqi_span:
            try:
                resp = requests.get(AIR_POLLUTION_API_URL, timeout=10)
                resp.raise_for_status()
                data = resp.json()

                # AQI is an integer 1–5: 1=Good, 2=Fair, 3=Moderate, 4=Poor, 5=Very Poor
                aqi = data["list"][0]["main"]["aqi"]

                # ── Exemplar recording ──────────────────────────────────────
                # Same pattern as temperature: recorded inside the active span
                # so the exemplar carries this span's trace_id.
                aqi_histogram.record(aqi, CITY_ATTRS)

                aqi_span.set_attributes({
                    "aqi.value": aqi,
                    "http.status_code": resp.status_code,
                })
                aqi_span.set_status(Status(StatusCode.OK))

                telemetry_record["air_quality_index"] = aqi
                msg = f"AQI: {aqi}"
                logger.info(msg)
                telemetry_record["log_messages"].append(msg)

            except requests.exceptions.RequestException as exc:
                error_counter.add(1, {**CITY_ATTRS, "source": "aqi"})
                msg = f"AQI fetch error: {exc}"
                logger.error(msg, exc_info=True)
                aqi_span.record_exception(exc)
                aqi_span.set_status(Status(StatusCode.ERROR, str(exc)))
                telemetry_record["log_messages"].append(msg)
            finally:
                fetch_counter.add(1, {**CITY_ATTRS, "source": "aqi"})

        # Mark the end of the collection cycle on the root span.
        span.add_event("finished_data_fetch")
        logger.info(
            "Fetch complete — temp=%(temp)s°C humidity=%(hum)s%% aqi=%(aqi)s",
            {
                "temp": telemetry_record["temperature_celsius"],
                "hum":  telemetry_record["humidity_percent"],
                "aqi":  telemetry_record["air_quality_index"],
            },
        )

    # ── Persist snapshot to disk ───────────────────────────────────────────────
    # Written AFTER the root span closes so the trace_id is finalised.
    # The Flask UI reads these files to display the latest readings.
    try:
        filename = os.path.join(
            DATA_DIR,
            f"telemetry_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.json",
        )
        with open(filename, "w") as f:
            json.dump(telemetry_record, f, indent=2)
        logger.info("Telemetry saved to %s", filename)
    except OSError as exc:
        logger.error("Failed to save telemetry file: %s", exc)


# ── Scheduler ─────────────────────────────────────────────────────────────────
# The schedule library runs jobs on the main thread. We move it to a daemon
# thread so the main thread can block on a KeyboardInterrupt cleanly.

SCHEDULE_INTERVAL_SECONDS = 10   # collect data every 10 seconds

if __name__ == "__main__":

    def _run_continuously(interval: int = 1) -> threading.Event:
        """
        Start the schedule runner in a background daemon thread.

        Returns a threading.Event that the caller can set() to stop the thread.
        The daemon=True flag ensures the thread is killed automatically when
        the main thread exits (e.g. after Ctrl+C).
        """
        stop_event = threading.Event()

        class _ScheduleThread(threading.Thread):
            def run(self):
                while not stop_event.is_set():
                    schedule.run_pending()
                    time.sleep(interval)

        t = _ScheduleThread(daemon=True)
        t.start()
        return stop_event

    # Register the collection job with the scheduler.
    schedule.clear()
    schedule.every(SCHEDULE_INTERVAL_SECONDS).seconds.do(fetch_weather_and_aqi)
    logger.info("Scheduled fetch every %s s. Press Ctrl+C to stop.", SCHEDULE_INTERVAL_SECONDS)

    # Start the background scheduler thread.
    stop = _run_continuously()

    # Run once immediately so we don't wait 10 s for the first reading.
    fetch_weather_and_aqi()

    # Keep the main thread alive until the user presses Ctrl+C.
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop.set()   # Signal the scheduler thread to exit cleanly.
        logger.info("Collector stopped.")
