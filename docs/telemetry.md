# Optional telemetry

CondenseIt can send traces, lifecycle events and metrics to an OpenTelemetry
collector over **OTLP/HTTP protobuf**. The collector can forward them to Langfuse,
Loki, Prometheus or another compatible backend.

## Connect a collector

Install `pip install '.[telemetry]'`, or use the Docker image, which includes
the telemetry dependencies. Exporting is disabled by default. Set these
environment variables in the process that runs `condenseit run` or
`condenseit serve`:

```sh
export CONDENSEIT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com
export OTEL_SERVICE_NAME=condenseit
export OTEL_RESOURCE_ATTRIBUTES=host.name=source-host,deployment.environment.name=production
export CONDENSEIT_OTEL_ENVIRONMENT=production
```

The base endpoint receives `/v1/traces`, `/v1/logs` and `/v1/metrics`. Load any
required `OTEL_EXPORTER_OTLP_HEADERS` from a secret at startup; use the standard
OTel header format, such as `Authorization=Bearer%20<token>`. Per-signal endpoint
and header overrides are also supported. Never commit credentials.

## What to expect

Each digest creates a unique session with nested pipeline, article and model
spans. Every span carries `application=condenseit`, `workflow=digest` and an
execution mode (`cli`, `web`, `scheduled` or `manual`). In Langfuse, filter by
tag `application:condenseit` or metadata `application = condenseit`.

Detailed model telemetry currently covers the OpenAI-compatible summarizer:
model identity, reported token usage, duration, retries and errors. Other
providers, embeddings and reranking have workflow spans only. Lifecycle events
carry trace/session IDs; metrics cover runs, latency, tokens and last success.
Costs are not inferred, and process-local counters reset after a restart.

Prompt/response capture is off by default. Enable it with
`CONDENSEIT_OTEL_CAPTURE_CONTENT=true`; `CONDENSEIT_OTEL_CONTENT_LIMIT` defaults
to 16,000 characters per field. Configure backend redaction before capturing
private content. Provider credentials and raw exception text are excluded.

To disable telemetry, unset `CONDENSEIT_OTEL_ENABLED` or set
`OTEL_SDK_DISABLED=true`, then restart the process. Telemetry uses bounded
in-memory queues with no disk spool; outages or process termination can lose
records. Missing SDK packages or invalid initialization settings leave digest
execution available with a warning.

## Test

Install `pip install '.[dev,telemetry]'` and run `python -m pytest`.
For a live integration,
run a digest and check records in the destination, including session grouping,
model usage and errors.
