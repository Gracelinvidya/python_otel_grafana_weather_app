# run_collector.ps1 — starts the weather collector
# Secrets are stored in .env (gitignored), NOT in this file.
# python-dotenv loads .env automatically when weather_collector.py starts.
# This script just ensures the .env file exists before launching.

$envFile = Join-Path $PSScriptRoot ".env"
if (-Not (Test-Path $envFile)) {
    Write-Error ".env file not found. Copy .env.example to .env and fill in your secrets."
    exit 1
}

python weather_collector.py
