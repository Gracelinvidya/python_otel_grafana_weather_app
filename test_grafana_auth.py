"""
test_grafana_auth.py — Diagnoses Grafana Cloud OTLP connectivity.

Reads credentials from .env (via python-dotenv) or from shell environment
variables. No secrets are hardcoded in this file.

Usage:
    python test_grafana_auth.py
"""

import base64
import os
import requests
from dotenv import load_dotenv

# Load .env so this script works standalone (outside the main app).
# override=False: shell env vars take precedence over .env values.
load_dotenv(override=False)

OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
TOKEN_RAW     = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")

if not OTLP_ENDPOINT or not TOKEN_RAW:
    print("ERROR: OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS must be set.")
    print("Copy .env.example → .env and fill in your Grafana Cloud credentials.")
    raise SystemExit(1)

# Decode the base64 token to extract instance ID (for display only — never print the password).
try:
    decoded      = base64.b64decode(TOKEN_RAW).decode()
    instance_id  = decoded.split(":")[0]
    token_prefix = decoded.split(":")[1][:20] if ":" in decoded else "???"
except Exception:
    instance_id  = "???"
    token_prefix = "???"

print("=" * 60)
print(f"Endpoint     : {OTLP_ENDPOINT}")
print(f"Instance ID  : {instance_id}")
print(f"Token prefix : {token_prefix}...  (first 20 chars only)")
print("=" * 60)

# Send a minimal protobuf body to verify the token is accepted.
url  = f"{OTLP_ENDPOINT}/v1/traces"
auth = ("Basic " + TOKEN_RAW) if not TOKEN_RAW.startswith("Basic ") else TOKEN_RAW
resp = requests.post(
    url,
    headers={
        "Authorization": auth,
        "Content-Type":  "application/x-protobuf",
    },
    data=b"\n\x00",   # empty but valid protobuf ResourceSpans message
    timeout=10,
)

print(f"\nOTLP /v1/traces  →  HTTP {resp.status_code}")
if resp.text:
    print(f"Response: {resp.text[:300]}")

if resp.status_code == 200:
    print("\n✅ Auth OK — data will reach Grafana")
elif resp.status_code == 401:
    print("\n❌ 401 Unauthorized — token is invalid or expired")
    print("   1. Go to your Grafana stack → Connections → Add new connection → OpenTelemetry (OTLP)")
    print("   2. Generate a new token with metrics:write, traces:write, logs:write scopes")
    print("   3. Update OTEL_EXPORTER_OTLP_HEADERS in your .env file")
elif resp.status_code == 403:
    print("\n❌ 403 Forbidden — token exists but lacks write permissions")
    print("   Re-generate the token and ensure all three write scopes are selected.")
else:
    print(f"\n⚠  Unexpected status {resp.status_code}")
