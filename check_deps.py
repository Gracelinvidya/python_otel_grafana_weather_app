deps = [
    "opentelemetry",
    "opentelemetry.sdk",
    "opentelemetry.sdk.metrics",
    "opentelemetry.sdk.trace",
    "opentelemetry.sdk.logs",
    "opentelemetry.instrumentation.requests",
    "schedule",
    "requests",
]
missing = []
for dep in deps:
    try:
        __import__(dep)
        print(f"OK: {dep}")
    except ImportError as e:
        print(f"MISSING: {dep} -> {e}")
        missing.append(dep)

if not missing:
    print("\nAll dependencies are installed.")
else:
    print(f"\nMissing: {missing}")
