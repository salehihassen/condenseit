# Optional AI telemetry

Install `pip install '.[telemetry]'`, or build the image with
`docker build --build-arg CONDENSEIT_EXTRAS=telemetry -t condenseit:otel .`.
The normal package and image do not require the OpenTelemetry SDK.

Telemetry is off by default. Enable it in the **actual launcher** (CLI,
container, or web server) with private runtime settings:

```sh
CONDENSEIT_OTEL_ENABLED=true
OTEL_SERVICE_NAME=condenseit
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel.private.example
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_RESOURCE_ATTRIBUTES=host.name=source-host,deployment.environment.name=production
CONDENSEIT_OTEL_ENVIRONMENT=production
```

Use your collector's authentication facility. The Python HTTP exporters honor
standard `OTEL_EXPORTER_OTLP_HEADERS` and per-signal endpoint/header overrides.
Load headers from a protected secret at process startup; do not store credentials
in source, an image, or command arguments. This integration always uses OTLP over
HTTP/protobuf. A generic base endpoint appends `/v1/traces`, `/v1/logs`, and
`/v1/metrics`; signal-specific endpoints must include the full path.
No Langfuse key is needed if the collector already supplies it.

Setting `OTEL_SDK_DISABLED=true` or leaving `CONDENSEIT_OTEL_ENABLED` unset
disables this integration, even when exporter settings are inherited. Missing
SDK packages or invalid initialization settings produce a sanitized warning and
leave digest execution available. The providers belong to CondenseIt and do not
replace global providers in an embedding application.

## Identity and Langfuse filtering

One `execute_digest()` invocation creates a new `condenseit-<uuid>` session and
an agent root span named `condenseit.digest`. Metadata and tags are attached
**before export to the collector**, on every CondenseIt span, including article
workers and generation spans:

| Field | Value |
| --- | --- |
| `application`, `langfuse.trace.metadata.application` | `condenseit` |
| `workflow`, `langfuse.trace.metadata.workflow` | `digest` |
| `gen_ai.agent.name` | `condenseit` |
| `execution_mode`, `langfuse.trace.metadata.execution_mode` | `cli`, `web`, `scheduled`, or `manual` |
| `session.id`, `langfuse.session.id` | Unique ID for this run |
| `langfuse.trace.tags` | `application:condenseit`, `workflow:digest`, `execution_mode:<mode>` |

Filter Langfuse Traces/Observations by tag `application:condenseit` or top-level
metadata `application = condenseit`, then group/open the corresponding session.
Use `execution_mode:scheduled` to select scheduler runs. In Langfuse v4 the same
metadata is needed on child observations; a tag only on the root is insufficient.
Host and environment come from the deployment's resource settings; a collector
may map the host into the Langfuse User field. CondenseIt is an application and
does not claim to be a coding harness such as Codex or Claude Code.

The tree includes pipeline, article, save, post-run and OpenAI-compatible model
operations. Each article task enters its own copy of the submitting context.
The OpenAI-compatible summarizer records requested/returned model, known input
and output tokens, cache/reasoning subsets when supplied, logical-call duration,
retry events and errors. Cached input is separated from uncached input in
Langfuse's explicit usage buckets; reasoning remains a subset of output.
Unknown usage is omitted. No dollar cost or time to first token is fabricated.
Other providers, embedding calls and reranking calls currently have enclosing
workflow spans but no model-specific instrumentation.

## Content, events and metrics

Prompts/responses are excluded by default. Explicitly set
`CONDENSEIT_OTEL_CAPTURE_CONTENT=true` to capture model messages/choices and the
root's arguments/result summary. `CONDENSEIT_OTEL_CONTENT_LIMIT` bounds each
content attribute to 16,000 characters by default (zero captures no characters).
No provider headers, keys or raw exception messages/stack traces are exported.
Content can still contain private information: configure collector redaction
before enabling it. Truncated serialized content may be plain text rather than
valid JSON. Authentication and output behavior of the digest are unchanged.

OTel log records describe run start/completion/failure and logical model-call
completion/failure, carrying trace/span context, session and application fields.
Model completion events include known token counts. Ordinary stdout/stderr
remains available to the existing container log collector.

Metrics include `condenseit.digest.runs`, `condenseit.digest.duration`,
`condenseit.digest.last_success` (Unix seconds), `condenseit.gen_ai.calls`,
`condenseit.gen_ai.tokens`, and `gen_ai.client.operation.duration`. Labels use
bounded execution modes/outcomes and model identity, never run/session IDs.
Prometheus commonly translates dots to underscores and adds counter/unit
suffixes. These are process-local measurements; counters reset after a restart.
Use the correlated completion events for durable per-call accounting.

`last_success` starts at zero and excludes dry runs. Monitor missed starts and
stale successes independently of the job's own failure events: a scheduler that
never launches cannot emit a failure span. Initialize the SDK in the server
launcher if a last-success sample is needed before the first job begins.

## Verification and loss boundary

Run `pytest tests/test_telemetry.py` with the telemetry and dev extras installed.
Tests exercise a real digest with a fake HTTP model, worker parentage, all-span
metadata, session isolation, retries, usage accounting and failure correlation.
Then send synthetic telemetry through the deployed collector and check stored
observations/events/metrics; HTTP 200 alone proves only ingress acceptance.
Finally run one bounded digest through its usual launcher and inspect its tree.

Export uses bounded in-memory queues (512 spans/records), two-second exporter
timeouts, 15-second periodic metric export, per-run flush and process shutdown.
Telemetry does not retry the digest or change its result. There is no client
disk spool or history replay: network outages, process kills and full queues
can lose telemetry before it reaches the collector. Collector persistence and
store retention/backup are separate deployment policies.

To roll back, disable `CONDENSEIT_OTEL_ENABLED` and restart the owning launcher;
the SDK dependency can remain installed. Remove only the app's private settings
and credential mount if desired; do not revoke a shared machine writer.

References: [Python OTel instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
and [Langfuse OTel mapping and propagation](https://langfuse.com/integrations/native/opentelemetry).
