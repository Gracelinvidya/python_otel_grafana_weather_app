# Chennai Weather Telemetry — Full Observability Stack

Real-time weather and air quality monitoring for Chennai, fully instrumented
with OpenTelemetry and visualised in Grafana Cloud.

Every metric data point carries an **exemplar** (embedded trace_id + span_id)
that creates clickable drilldown links between metrics, traces, and logs — all
three signals are correlated by a shared `trace_id`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Repository Structure](#2-repository-structure)
3. [How the Three Signals Are Correlated](#3-how-the-three-signals-are-correlated)
4. [Exemplar Drilldown Chain](#4-exemplar-drilldown-chain)
5. [Prerequisites](#5-prerequisites)
6. [Installation](#6-installation)
7. [Configuration](#7-configuration)
8. [Running the Stack](#8-running-the-stack)
9. [Metrics Reference](#9-metrics-reference)
10. [Trace Span Reference](#10-trace-span-reference)
11. [Log Fields Reference](#11-log-fields-reference)
12. [Grafana Dashboard](#12-grafana-dashboard)
13. [Grafana Datasource Setup](#13-grafana-datasource-setup)
14. [4 Golden Signals Explained](#14-4-golden-signals-explained)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Your Machine                               │
│                                                                     │
│  ┌──────────────────────────┐    ┌──────────────────────────────┐  │
│  │   weather_collector.py   │    │    flask_weather_app.py      │  │
│  │                          │    │                              │  │
│  │  Every 10 s:             │    │  GET /          →  index     │  │
│  │  ├─ fetch /weather API   │    │  GET /data/<f>  →  JSON API  │  │
│  │  └─ fetch /air_pollution │    │                              │  │
│  │                          │    │  Reads JSON snapshots        │  │
│  │  Writes JSON snapshots ──┼───►│  written by collector        │  │
│  │  to ./telemetry_data/    │    │                              │  │
│  └──────────┬───────────────┘    └──────────────┬───────────────┘  │
│             │                                   │                  │
│             └──────────────┬────────────────────┘                  │
│                            │  OTel SDK (otel_config.py)            │
│                            │  ┌────────────────────────────────┐   │
│                            │  │  TracerProvider                │   │
│                            │  │  MeterProvider (exemplars ON)  │   │
│                            │  │  LoggerProvider                │   │
│                            │  └────────────────────────────────┘   │
└────────────────────────────┼────────────────────────────────────────┘
                             │  OTLP/HTTP  (protobuf, Basic Auth)
                             │
                             ▼
          ┌──────────────────────────────────────────────┐
          │         Grafana Cloud OTLP Gateway           │
          │  otlp-gateway-prod-ap-south-1.grafana.net    │
          │                                              │
          │   /v1/traces  ──►  Grafana Tempo             │
          │   /v1/metrics ──►  Grafana Mimir (Prometheus)│
          │   /v1/logs    ──►  Grafana Loki              │
          └──────────────────────────────────────────────┘
                             │
                             ▼
          ┌──────────────────────────────────────────────┐
          │          Grafana Cloud Dashboard             │
          │   brightattic2654.grafana.net                │
          │                                              │
          │   4 Golden Signals dashboard                 │
          │   Exemplar scatter dots → trace drilldown    │
          │   Trace table → Loki log drilldown           │
          │   Log line TraceID pill → Tempo waterfall    │
          └──────────────────────────────────────────────┘
```

---

## 2. Repository Structure

```
Kiro_proj/
│
├── otel_config.py            # Shared OTel bootstrap (all 3 signal pipelines)
├── weather_collector.py      # Data collector: fetches weather + AQI, emits OTel
├── flask_weather_app.py      # Flask UI: serves telemetry data, emits OTel
│
├── run_collector.ps1         # PowerShell launcher for the collector
├── run_flask.ps1             # PowerShell launcher for the Flask app
│
├── grafana_dashboard.json    # Importable Grafana dashboard (4 Golden Signals)
├── build_dashboard.py        # Script that generated grafana_dashboard.json
│
├── test_grafana_auth.py      # Connectivity test: verifies token → HTTP 200
├── test_full_otel.py         # End-to-end OTel test: sends one trace+metric+log
├── check_deps.py             # Checks all required Python packages are installed
├── check_dashboard.py        # Validates grafana_dashboard.json structure
│
└── telemetry_data/           # JSON snapshots written by weather_collector.py
    └── telemetry_<ts>.json
```

### File responsibilities

| File | Responsibility |
|------|---------------|
| `otel_config.py` | Single place that creates TracerProvider, MeterProvider, LoggerProvider and exports them to Grafana Cloud. Both apps import `init_otel()` from here. |
| `weather_collector.py` | Polls OpenWeatherMap every 10 s. Emits spans (`fetch_weather_data`, `fetch_weather`, `fetch_aqi`), histograms with exemplars, and structured logs. |
| `flask_weather_app.py` | Flask web server. Auto-instrumented by FlaskInstrumentor. Emits request-duration histograms with exemplars and structured logs. |
| `grafana_dashboard.json` | Ready-to-import Grafana dashboard covering all 4 golden signals with full exemplar drilldown chain. |

---

## 3. How the Three Signals Are Correlated

All three OTel signals share a single `trace_id` per collection cycle or HTTP
request. This is what makes drilldowns work — every signal can find the others
via this common identifier.

```
One collection cycle (weather_collector.py)
│
├── TRACE  trace_id = "abc123..."
│     ├── span: fetch_weather_data   (root)
│     │     ├── span: fetch_weather  (child)
│     │     └── span: fetch_aqi      (child)
│     └── auto-spans from RequestsInstrumentor for each HTTP call
│
├── METRICS  (recorded INSIDE the active spans above)
│     ├── outdoor_temperature_celsius  { exemplar.traceID = "abc123..." }
│     ├── outdoor_humidity_percent     { exemplar.traceID = "abc123..." }
│     ├── air_quality_index            { exemplar.traceID = "abc123..." }
│     └── weather_fetch_total          { exemplar.traceID = "abc123..." }
│
└── LOGS  (emitted via logger.info/error() inside the active spans)
      ├── "Starting weather+AQI fetch"   { trace_id = "abc123...", span_id = "def456..." }
      ├── "Weather: 32°C, Humidity: 78%" { trace_id = "abc123...", span_id = "..." }
      └── "AQI: 2"                       { trace_id = "abc123...", span_id = "..." }
```

The `trace_id` is injected into logs automatically by the OTel `LoggingHandler`
bridge (`otel_config.attach_otel_logging_handler`). It is embedded into metric
exemplars automatically by `TraceBasedExemplarFilter` whenever a
`.record()` or `.add()` call is made while a sampled span is active on the
current thread.

---

## 4. Exemplar Drilldown Chain

This is the full navigation path available in the Grafana dashboard:

```
Metric chart (e.g. outdoor_temperature_celsius)
    │
    ▼  click a scatter dot (exemplar)
    │
    ├──► "→ Tempo: view trace"
    │         Opens the full span waterfall for the collection cycle that
    │         produced this data point. You can see:
    │           • fetch_weather_data  (root span, ~200 ms total)
    │           •   fetch_weather     (child, HTTP GET to OpenWeatherMap)
    │           •   fetch_aqi         (child, HTTP GET to OpenWeatherMap)
    │           •   auto HTTP spans   (from RequestsInstrumentor)
    │
    └──► "→ Loki: logs for this trace"
              Opens Loki Explore filtered to:
                {service_name="weather-collector-chennai"} | trace_id = "abc123..."
              Shows every log line emitted during that exact collection cycle.


Trace table row (Traces section of the dashboard)
    │
    ▼  click the traceID cell
    │
    ├──► "→ Tempo: open trace waterfall"   (same as above)
    └──► "→ Loki: logs for this trace"      (same as above)


Log line (Logs section of the dashboard)
    │
    ▼  expand a log entry → click the "TraceID" derived-field pill
    │
    └──► Tempo: opens the trace waterfall for this log line's parent span
```

### Why histograms instead of gauges?

The OTel SDK's `TraceBasedExemplarFilter` only fires on instruments that
support exemplars: **histograms** and **counters**. A gauge's `.set()` call
does not go through the exemplar filter, so gauges cannot carry drilldown
links. All sensor readings (temperature, humidity, AQI) use histograms for
this reason.

---

## 5. Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.10+ | 3.14 tested |
| pip packages | see below | |
| Grafana Cloud account | Free tier | [grafana.com](https://grafana.com) |
| OpenWeatherMap API key | Free tier | [openweathermap.org](https://openweathermap.org/api) |

---

## 6. Installation

```powershell
# Clone or open the project
cd c:\VIDYA\Kiro_proj

# Install all required packages (including python-dotenv for .env loading)
pip install flask==3.1.3 `
    requests==2.34.2 `
    schedule==1.2.2 `
    python-dotenv `
    opentelemetry-api==1.45.0 `
    opentelemetry-sdk==1.45.0 `
    opentelemetry-exporter-otlp-proto-http==1.45.0 `
    opentelemetry-instrumentation-requests==0.66b0 `
    opentelemetry-instrumentation-flask==0.66b0

# Verify all packages are present
python check_deps.py
```

---

## 7. Configuration & Secret Management

### The best way to store secrets: `.env` file + `.gitignore`

All credentials live in a single `.env` file that is **never committed to git**.
The pattern used here is the industry standard for local development:

```
.env          ← real secrets  (gitignored — never commit)
.env.example  ← safe template (committed — shows what keys are needed)
.gitignore    ← ensures .env is always excluded from version control
```

`python-dotenv` loads `.env` automatically at startup via `load_dotenv()` in
`otel_config.py`. No secrets ever appear in source code or launcher scripts.

### Setup steps

**1. Create your `.env` file**

```powershell
Copy-Item .env.example .env
```

**2. Fill in your secrets**

Open `.env` in any editor and set the three values:

```ini
# OpenWeatherMap — free key from https://openweathermap.org/api
OPENWEATHERMAP_API_KEY=your_key_here

# Grafana Cloud — from your stack:
#   Connections → Add new connection → OpenTelemetry (OTLP)
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp-gateway-prod-ap-south-1.grafana.net/otlp
OTEL_EXPORTER_OTLP_HEADERS=your_grafana_token_here
```

**3. Get your Grafana Cloud token**

1. Log in at [https://grafana.com](https://grafana.com)
2. Open your stack → **Connections** → **Add new connection**
3. Search for **OpenTelemetry (OTLP)**
4. Click **"Create a Grafana Cloud stack OTLP connection"**
5. Copy the value next to `OTEL_EXPORTER_OTLP_HEADERS` into your `.env`

**4. Verify connectivity**

```powershell
python test_grafana_auth.py
# Expected: HTTP 200  ✅ Auth OK — data will reach Grafana
```

### How `load_dotenv` works

```
App starts
    │
    └─ otel_config.py imported
            │
            └─ load_dotenv(override=False)
                    │
                    ├─ Reads .env if it exists
                    ├─ Sets each key into os.environ
                    └─ override=False: shell env vars always win
                       (CI/CD platforms inject secrets at the shell level
                        and don't need a .env file at all)
```

If `.env` is missing and the variables are not set in the shell, `otel_config.py`
raises an `EnvironmentError` immediately with a clear message telling you exactly
which variable is missing and how to fix it.

### Secret management options (from simplest to most secure)

| Approach | Best for | How |
|----------|----------|-----|
| `.env` file + `python-dotenv` | Local development | This project's approach |
| Shell environment variables | CI/CD pipelines (GitHub Actions, etc.) | `$env:VAR=value` in PowerShell; `export VAR=value` in bash |
| Windows Credential Manager | Single-developer Windows machines | `cmdkey` / `Get-StoredCredential` |
| Azure Key Vault / AWS Secrets Manager | Production / team environments | SDK pulls secrets at runtime |
| HashiCorp Vault | Enterprise / multi-team | Vault agent injects secrets as env vars |

### What is gitignored

`.gitignore` excludes:
- `.env` — secrets
- `telemetry_data/` — runtime-generated JSON snapshots (Grafana is the source of truth)
- `__pycache__/` — compiled bytecode
- `.venv/` — virtual environments

---

## 8. Running the Stack

Open **two separate PowerShell terminals**.

### Terminal 1 — Weather Collector

```powershell
cd c:\VIDYA\Kiro_proj
powershell -ExecutionPolicy Bypass -File run_collector.ps1
```

Expected output:
```
2026-10-01 03:02:05 INFO Scheduled fetch every 10 s. Press Ctrl+C to stop.
2026-10-01 03:02:05 INFO Starting weather+AQI fetch for Chennai
2026-10-01 03:02:06 INFO Weather: 32.4°C, Humidity: 78%
2026-10-01 03:02:06 INFO AQI: 2
2026-10-01 03:02:06 INFO Telemetry saved to ./telemetry_data/telemetry_20261001030206.json
```

### Terminal 2 — Flask Web App

```powershell
cd c:\VIDYA\Kiro_proj
powershell -ExecutionPolicy Bypass -File run_flask.ps1
```

Expected output:
```
 * Serving Flask app 'flask_weather_app'
 * Debug mode: off
 * Running on http://0.0.0.0:5000
```

Open **http://localhost:5000** in your browser. The page auto-refreshes every
10 seconds showing the latest reading.

> Both scripts check that `.env` exists before starting and exit with a clear
> error if it is missing. Secrets are loaded by `python-dotenv` inside the
> Python process — they are never exposed in the PowerShell process list or logs.

### Stopping

Press `Ctrl+C` in each terminal.

---

## 9. Metrics Reference

All metrics are exported to Grafana Cloud Metrics (Prometheus/Mimir) every
15 seconds via `PeriodicExportingMetricReader`.

### weather-collector-chennai

| Metric name | Type | Unit | Labels | Description |
|-------------|------|------|--------|-------------|
| `outdoor_temperature_celsius` | Histogram | Cel | `city` | Temperature reading. Exemplar carries `fetch_weather` span. |
| `outdoor_humidity_percent` | Histogram | % | `city` | Humidity reading. Exemplar carries `fetch_weather` span. |
| `air_quality_index` | Histogram | AQI | `city` | AQI reading (1–5). Exemplar carries `fetch_aqi` span. |
| `weather_fetch_total` | Counter | — | `city`, `source` | Total fetch attempts. `source` = `weather` or `aqi`. |
| `weather_fetch_errors_total` | Counter | — | `city`, `source` | Failed fetch attempts only. |

**Error ratio query** (use in Grafana alerts):
```promql
sum(rate(weather_fetch_errors_total[5m]))
/
sum(rate(weather_fetch_total[5m]))
```

### flask-weather-app

| Metric name | Type | Unit | Labels | Description |
|-------------|------|------|--------|-------------|
| `http_requests_total` | Counter | — | `route`, `method` | Total HTTP requests received. |
| `http_request_duration_seconds` | Histogram | s | `route`, `method`, `status` | End-to-end request latency. Exemplar carries the request's span. |
| `telemetry_files_loaded` | Histogram | files | `route` | Files on disk per index request. |
| `telemetry_file_not_found_total` | Counter | — | `filename` | 404 count per requested filename. |

**p99 latency query**:
```promql
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds_bucket{service_name="flask-weather-app"}[5m]))
  by (le, route)
)
```

---

## 10. Trace Span Reference

All spans are exported to Grafana Tempo via `BatchSpanProcessor` →
`OTLPSpanExporter` → `/v1/traces`.

### weather-collector-chennai

```
fetch_weather_data                          ← root span, one per collection cycle
│   Attributes: city, lat, lon
│   Events: starting_data_fetch, finished_data_fetch
│
├── fetch_weather                           ← child span
│   │   Attributes: weather.temperature_celsius, weather.humidity_percent,
│   │               http.status_code
│   │   Status: OK or ERROR
│   │
│   └── GET https://api.openweathermap.org/data/2.5/weather   ← auto (RequestsInstrumentor)
│           Attributes: http.url, http.method, http.status_code, http.flavor
│
└── fetch_aqi                               ← child span
    │   Attributes: aqi.value, http.status_code
    │   Status: OK or ERROR
    │
    └── GET https://api.openweathermap.org/data/2.5/air_pollution ← auto
```

### flask-weather-app

```
GET /                                       ← auto span (FlaskInstrumentor)
│   Attributes: http.method, http.route, http.status_code, http.url, http.flavor
│
└── load_latest_telemetry                   ← manual child span
        Attributes: telemetry.file_count, telemetry.latest_file

GET /data/<filename>                        ← auto span
│
└── serve_telemetry_file                    ← manual child span
        Attributes: requested.filename, file.found
```

### Span status codes

| StatusCode | When set |
|------------|----------|
| `OK` | API call succeeded, data parsed successfully |
| `ERROR` | `requests.exceptions.RequestException` caught; `record_exception()` also called, storing the full stack trace in the span |

---

## 11. Log Fields Reference

All logs are exported to Grafana Loki via `BatchLogRecordProcessor` →
`OTLPLogExporter` → `/v1/logs`. The OTel `LoggingHandler` bridge
automatically injects these fields into every record:

| Field | Source | Example value |
|-------|--------|---------------|
| `service_name` | Resource | `weather-collector-chennai` |
| `trace_id` | Active span context | `abc123def456...` (32 hex chars) |
| `span_id` | Active span context | `def456...` (16 hex chars) |
| `severity` | Python log level | `INFO`, `ERROR`, `WARNING` |
| `body` | Log message string | `Weather: 32.4°C, Humidity: 78%` |

### Useful Loki queries

```logql
# All logs for a service
{service_name="weather-collector-chennai"}

# Errors only
{service_name="weather-collector-chennai"} | severity = "ERROR"

# Logs for a specific trace (from exemplar drilldown)
{service_name="weather-collector-chennai"} | trace_id = "abc123..."

# Both services, keyword search
{service_name=~"weather.*|flask.*"} |= "AQI"
```

---

## 12. Grafana Dashboard

The file `grafana_dashboard.json` is a ready-to-import Grafana dashboard
covering all **4 Golden Signals** with full exemplar-based drilldown.

### Import steps

1. Open [https://brightattic2654.grafana.net/](https://brightattic2654.grafana.net/)
2. Left sidebar → **Dashboards** → **New** → **Import**
3. Click **"Upload dashboard JSON file"** → select `grafana_dashboard.json`
4. Map the three datasource inputs:

   | Input name | Map to |
   |------------|--------|
   | `DS_PROMETHEUS` | Your Grafana Cloud Metrics datasource |
   | `DS_TEMPO` | Your Grafana Cloud Traces (Tempo) datasource |
   | `DS_LOKI` | Your Grafana Cloud Logs (Loki) datasource |

5. Click **Import**

### Dashboard sections

| Row | Golden Signal | Panels |
|-----|--------------|--------|
| 🚦 Traffic | Request rate | Flask HTTP req/s by route; Collector fetch/s by source |
| 🔴 Errors | Error rate | Collector fetch error ratio; Flask 404 rate |
| ⏱ Latency | Request duration | Flask p50/p95/p99 latency; Sensor reading distribution |
| 🌡 Saturation | Capacity usage | Temperature gauge; Humidity gauge; AQI gauge; Files served stat |
| 🔍 Traces | Trace drilldown | Recent traces table for each service (traceID links to Tempo + Loki) |
| 📋 Logs | Log drilldown | Live log panels for each service (TraceID pill links to Tempo) |

### Dashboard variables

| Variable | Type | Usage |
|----------|------|-------|
| `$service` | Query (multi-select) | Filter metric panels by service name |
| `$log_search` | Text box | Keyword filter applied to both Loki log panels simultaneously |

---

## 13. Grafana Datasource Setup

### Tempo → Logs (trace waterfall "Logs" tab)

For the "Logs for this span" button to appear inside the Tempo trace waterfall,
configure the Tempo datasource manually:

1. Grafana → **Connections** → **Data sources** → select your Tempo datasource
2. Scroll to **Trace to logs**
3. Set:
   - **Data source**: select your Loki datasource
   - **Tags**: `service.name`
   - **Mapped tags**: key = `service.name`, value = `service_name`
   - **Filter by trace ID**: ✅ enabled
   - **Filter by span ID**: ☐ disabled
   - **Use custom query**: ✅ enabled
   - **Query**: `{service_name="${__span.tags.service_name}"} | trace_id = "${__trace.traceId}"`
4. Click **Save & test**

### Loki → Tempo (TraceID derived field)

The dashboard already configures `dataLinks` on log panels with a regex
`trace_id=(\w+)` to extract and linkify trace IDs. If you want this globally
for all Loki usage, also configure it in the datasource:

1. Grafana → **Connections** → **Data sources** → select your Loki datasource
2. Scroll to **Derived fields**
3. Add:
   - **Name**: `TraceID`
   - **Regex**: `trace_id=(\w+)`
   - **URL**: `${DS_TEMPO}/explore?traceId=${__value.raw}`
   - **Internal link**: ✅ enabled → select your Tempo datasource
4. Click **Save & test**

---

## 14. 4 Golden Signals Explained

The **4 Golden Signals** (from the Google SRE Book) are the minimum set of
metrics needed to understand the health of a service. Here is how each one
maps to this project:

### 1. Traffic — "How much demand is the system receiving?"

Measured by **request rate** and **fetch rate**.

```promql
# Flask: HTTP requests per second
sum(rate(http_requests_total{service_name="flask-weather-app"}[5m])) by (route)

# Collector: API fetches per second
sum(rate(weather_fetch_total{service_name="weather-collector-chennai"}[5m])) by (source)
```

A sudden drop in traffic may indicate the collector has stopped or the Flask
app has crashed. A spike may indicate unexpected load on the web UI.

### 2. Errors — "What fraction of requests are failing?"

Measured by **fetch error ratio** and **404 rate**.

```promql
# Collector error ratio (0 = all good, 1 = all failing)
sum(rate(weather_fetch_errors_total[5m])) / sum(rate(weather_fetch_total[5m]))

# Flask 404 rate
sum(rate(telemetry_file_not_found_total{service_name="flask-weather-app"}[5m]))
```

When the error ratio rises above 0, click the exemplar dot to immediately
open the failing trace in Tempo and see the full exception stack trace on the
span, then pivot to Loki to read the `logger.error()` message with context.

### 3. Latency — "How long does it take to serve requests?"

Measured by **histogram percentiles** of request duration and sensor fetch time.

```promql
# Flask p99 latency by route
histogram_quantile(0.99,
  sum(rate(http_request_duration_seconds_bucket{service_name="flask-weather-app"}[5m]))
  by (le, route)
)
```

The exemplar on the p99 line points to the single slowest request in that
window. Clicking opens the trace waterfall showing which part of the code was
slow (framework overhead vs. file I/O vs. JSON parsing).

### 4. Saturation — "How full is the system?"

Measured by **sensor reading gauges** and **files on disk**.

```promql
# Current temperature (latest histogram bucket median)
histogram_quantile(0.50, sum(rate(outdoor_temperature_celsius_bucket[5m])) by (le))

# Files accumulated on disk (proxy for "is the collector keeping up?")
sum(increase(telemetry_files_loaded_sum{service_name="flask-weather-app"}[5m]))
```

High AQI or temperature values indicate environmental stress. A large and
growing file count indicates the telemetry_data directory is not being pruned.

---

## 15. Troubleshooting

### No data appears in Grafana

Run the auth test:
```powershell
python test_grafana_auth.py
```
- **HTTP 200** → auth OK, wait ~60 s for first flush
- **HTTP 401** → token expired; generate a new one from the Grafana Cloud OTLP page
- **HTTP 403** → token exists but missing `metrics:write`, `traces:write`, or `logs:write` scope

Run the end-to-end test to confirm OTel → Grafana flow works:
```powershell
python test_full_otel.py
# Then check Grafana Explore for service_name="otel-test-service"
```

### Metrics visible but no exemplar dots

- Confirm the metric panel has **"Exemplars"** toggle enabled in Grafana panel settings (Edit panel → Data → enable exemplar toggle per query).
- Ensure `.record()` calls happen **inside** an active span. If they are called outside any `with tracer.start_as_current_span(...)` block, `TraceBasedExemplarFilter` will not attach an exemplar.
- Check that the tracer is sampling: by default the SDK uses `ParentBasedTraceIdRatioBased` which samples 100% of root spans.

### Exemplar links open empty Tempo / Loki pages

- In Grafana Explore → Tempo, confirm the correct datasource is selected.
- Verify `DS_TEMPO` and `DS_LOKI` were mapped correctly during dashboard import.
- Check the Tempo datasource has **Trace to logs** configured (see [Section 13](#13-grafana-datasource-setup)).

### Collector stops writing files

```powershell
# Check the process is still running
Get-Process python

# Check recent file writes
Get-ChildItem .\telemetry_data | Sort-Object LastWriteTime -Descending | Select-Object -First 5
```

### Flask app returns 404 for all files

This usually means the collector is not running and `./telemetry_data/` is
empty. Start the collector first, wait 10 seconds, then refresh the Flask page.

### Port 5000 already in use

```powershell
# Find what is using port 5000
netstat -ano | findstr :5000

# Kill the process (replace <PID> with the actual PID)
Stop-Process -Id <PID> -Force
```

---

## Signal Flow Summary

```
OpenWeatherMap API
        │
        │  HTTP GET (every 10 s)
        ▼
weather_collector.py
        │
        ├── creates root span: fetch_weather_data ──────────────────────┐
        │        │                                                       │
        │        ├── child span: fetch_weather                          │
        │        │     └── histogram.record(temperature) ──► exemplar   │
        │        │     └── histogram.record(humidity)    ──► exemplar   │
        │        │     └── logger.info("Weather: 32°C")  ──► trace_id   │
        │        │                                                       │
        │        └── child span: fetch_aqi                              │
        │              └── histogram.record(aqi)         ──► exemplar   │
        │              └── logger.info("AQI: 2")         ──► trace_id   │
        │                                                               │
        │  All signals share the SAME trace_id ◄────────────────────────┘
        │
        ├── OTLP/HTTP → /v1/traces  → Grafana Tempo
        ├── OTLP/HTTP → /v1/metrics → Grafana Mimir
        └── OTLP/HTTP → /v1/logs   → Grafana Loki
                                            │
                              All queryable in the
                              4 Golden Signals Dashboard
                              with exemplar drilldown links
```
