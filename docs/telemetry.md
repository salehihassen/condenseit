# Optional LLM tracing

Normal Python and Docker installs include the telemetry dependencies.
Exporting is **off by default**. To send traces to an OpenTelemetry collector
using OTLP/HTTP protobuf:

```sh
export CONDENSEIT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example.com
export OTEL_SERVICE_NAME=condenseit
export OTEL_RESOURCE_ATTRIBUTES=host.name=source-host,deployment.environment.name=production
```

The SDK appends `/v1/traces` to the base endpoint. Load any required
`OTEL_EXPORTER_OTLP_HEADERS` from a secret at startup using standard OTel header
syntax, e.g. `Authorization=Bearer%20<token>`. Standard trace endpoint/header
overrides also work. Never commit credentials.

Each digest has its own trace/session, with child model-call spans for the
**OpenAI-compatible summarizer**. These include requested/returned model,
duration, reported token usage and error type. Retries remain within one model
span. Other providers, embeddings and reranking are not instrumented.

Every span carries `application=condenseit` and `workflow=digest`. Langfuse
session grouping and tag `application:condenseit` are included; optionally set
`CONDENSEIT_OTEL_ENVIRONMENT` (default `production`). No backend-specific keys
are needed when exporting through a collector.

Prompt/response capture requires `CONDENSEIT_OTEL_CAPTURE_CONTENT=true` and is
limited to 16,000 characters per field. Configure collector redaction before
capturing private content. Provider keys and raw exception text are excluded.

Unset `CONDENSEIT_OTEL_ENABLED`, set it to `false`, or set
`OTEL_SDK_DISABLED=true`, then restart to disable tracing. Missing SDK packages or
invalid initialization settings leave inference available with a warning.
Exports use bounded in-memory queues and timeouts; outages can lose spans.

Test with `pip install '.[dev]'` and `python -m pytest`.
