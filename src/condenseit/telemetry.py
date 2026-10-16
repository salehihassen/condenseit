"""Optional OTLP telemetry. No SDK imports or exporters unless explicitly enabled.

Providers are owned by this module rather than installed as global providers.
Run context is local to the execution and copied separately into each worker.
Langfuse attributes are an adapter on standard OTel spans; no project keys or
Langfuse dependency are required when exporting through a collector.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
from importlib.metadata import version
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
_context: ContextVar[dict[str, str] | None] = ContextVar(
    "digest_telemetry", default=None
)
_lock = threading.Lock()
_runtime: Runtime | None = None
_configured = False
_T = TypeVar("_T")


class Runtime:
    """Own the three SDK providers and bounded, process-local export queues."""

    def __init__(self, traces: Any, logs: Any, metrics: Any) -> None:
        self.providers = (traces, logs, metrics)
        self.tracer = traces.get_tracer("condenseit", version("condenseit"))
        self.events = logs.get_logger("condenseit")
        meter = metrics.get_meter("condenseit")
        self.runs = meter.create_counter("condenseit.digest.runs", unit="{run}")
        self.duration = meter.create_histogram("condenseit.digest.duration", unit="s")
        self.calls = meter.create_counter("condenseit.gen_ai.calls", unit="{call}")
        self.latency = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s"
        )
        self.tokens = meter.create_counter("condenseit.gen_ai.tokens", unit="{token}")
        self.last_success = 0.0
        meter.create_observable_gauge(
            "condenseit.digest.last_success", callbacks=[self._last_success], unit="s"
        )
        self.capture = (
            os.getenv("CONDENSEIT_OTEL_CAPTURE_CONTENT", "").lower() == "true"
        )
        self.content_limit = max(
            0, int(os.getenv("CONDENSEIT_OTEL_CONTENT_LIMIT", "16000"))
        )

    def _last_success(self, _options: Any) -> Any:
        from opentelemetry.metrics import Observation as MetricObservation

        yield MetricObservation(self.last_success, {"application": "condenseit"})

    def emit(self, event: str, attributes: dict[str, Any] | None = None) -> None:
        from opentelemetry._logs import SeverityNumber

        self.events.emit(
            body=event,
            attributes={**identity(), **(attributes or {})},
            severity_number=(
                SeverityNumber.ERROR
                if event.endswith(".failed")
                else SeverityNumber.INFO
            ),
        )

    def flush(self) -> None:
        # Each provider gets a fixed budget. Exporters also have bounded timeouts.
        for provider in self.providers:
            try:
                provider.force_flush(timeout_millis=2000)
            except Exception:
                logger.warning("Telemetry flush failed; digest results are unaffected")

    def shutdown(self) -> None:
        for provider in self.providers:
            try:
                provider.shutdown()
            except Exception:
                logger.warning("Telemetry shutdown failed")


def _configure() -> Runtime:
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # Resource.create merges OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME.
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "condenseit"),
            "service.version": version("condenseit"),
        }
    )
    with ExitStack() as cleanup:
        traces = TracerProvider(resource=resource)
        cleanup.callback(traces.shutdown)
        traces.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(timeout=2),
                max_queue_size=512,
                schedule_delay_millis=1000,
            )
        )
        logs = LoggerProvider(resource=resource)
        cleanup.callback(logs.shutdown)
        logs.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(timeout=2),
                max_queue_size=512,
                schedule_delay_millis=1000,
            )
        )
        metrics = MeterProvider(
            resource=resource,
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(timeout=2),
                    export_interval_millis=15000,
                    export_timeout_millis=2000,
                )
            ],
        )
        cleanup.callback(metrics.shutdown)
        runtime = Runtime(traces, logs, metrics)
        cleanup.pop_all()
    atexit.register(runtime.shutdown)
    return runtime


def get_runtime() -> Runtime | None:
    global _configured, _runtime
    if (
        os.getenv("CONDENSEIT_OTEL_ENABLED", "").lower() != "true"
        or os.getenv("OTEL_SDK_DISABLED", "").lower() == "true"
    ):
        return None
    with _lock:
        if not _configured:
            _configured = True
            try:
                _runtime = _configure()
            except Exception as error:
                # Never include exporter configuration, headers or exception text.
                logger.warning(
                    "Telemetry unavailable (%s); install condenseit[telemetry] "
                    "and check OTLP settings",
                    type(error).__name__,
                )
    return _runtime


def identity() -> dict[str, Any]:
    context = _context.get() or {}
    attributes: dict[str, Any] = {
        "application": "condenseit",
        "workflow": "digest",
        "gen_ai.agent.name": "condenseit",
        "langfuse.trace.name": "condenseit.digest",
        "langfuse.trace.tags": ["application:condenseit", "workflow:digest"],
        "langfuse.trace.metadata.application": "condenseit",
        "langfuse.trace.metadata.workflow": "digest",
    }
    for key, value in context.items():
        attributes[key] = value
        if key == "execution_mode":
            attributes["langfuse.trace.metadata.execution_mode"] = value
            attributes["langfuse.trace.tags"].append("execution_mode:" + value)
        elif key == "session.id":
            attributes["langfuse.session.id"] = value
    environment = os.getenv("CONDENSEIT_OTEL_ENVIRONMENT", "")
    if environment:
        attributes["langfuse.environment"] = environment
    return attributes


class Observation:
    """Small no-op facade so application call sites never require the SDK."""

    def __init__(self, span: Any = None, runtime: Runtime | None = None) -> None:
        self.span = span
        self.runtime = runtime
        self.usage: dict[str, int] = {}
        self.model = ""

    def set(self, key: str, value: Any) -> None:
        if self.span is not None:
            self.span.set_attribute(key, value)

    def content(self, key: str, value: Any) -> None:
        if self.runtime is not None and self.runtime.capture:
            text = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False)
            )
            self.set("langfuse.observation." + key, text[: self.runtime.content_limit])

    def retry(self, attempt: int, status: int) -> None:
        if self.span is not None:
            self.set("retry.count", attempt)
            self.span.add_event(
                "model.retry",
                {"retry.attempt": attempt, "http.response.status_code": status},
            )

    def response(self, data: dict[str, Any]) -> None:
        if self.runtime is None:
            return
        model = self.model = str(data.get("model") or "")
        if model:
            self.set("gen_ai.response.model", model)
            self.set("langfuse.observation.model.name", model)
        self.content("output", data.get("choices", []))
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        for source, target, kind in [
            ("prompt_tokens", "input_tokens", "input"),
            ("completion_tokens", "output_tokens", "output"),
        ]:
            value = usage.get(source)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.set("gen_ai.usage." + target, value)
                self.usage[target] = value
                self.runtime.tokens.add(
                    value, {"gen_ai.token.type": kind, "gen_ai.request.model": model}
                )
        # Cache/reasoning counts are subsets of the above totals, never added again.
        for source, detail, target in [
            ("prompt_tokens_details", "cached_tokens", "cache_read_input_tokens"),
            ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
        ]:
            details = usage.get(source)
            value = details.get(detail) if isinstance(details, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.set("gen_ai.usage." + target, value)
                self.usage[target] = value
        # Explicit Langfuse buckets avoid counting cached/reasoning subsets twice.
        if "input_tokens" in self.usage and "output_tokens" in self.usage:
            cached = min(
                self.usage.get("cache_read_input_tokens", 0), self.usage["input_tokens"]
            )
            self.set(
                "langfuse.observation.usage_details",
                json.dumps(
                    {
                        "input": self.usage["input_tokens"] - cached,
                        "input_cached": cached,
                        "output": self.usage["output_tokens"],
                        "total": self.usage["input_tokens"]
                        + self.usage["output_tokens"],
                    }
                ),
            )


@contextmanager
def span(
    name: str, kind: str = "chain", attributes: dict[str, Any] | None = None
) -> Iterator[Observation]:
    runtime = get_runtime()
    if runtime is None:
        yield Observation()
        return
    from opentelemetry.trace import StatusCode

    with runtime.tracer.start_as_current_span(
        name,
        attributes={
            **identity(),
            "langfuse.observation.type": kind,
            **(attributes or {}),
        },
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield Observation(current, runtime)
        except BaseException as error:
            # Exception strings/stack traces can contain URL credentials or content.
            current.set_attribute("error.type", type(error).__name__)
            current.set_status(StatusCode.ERROR, type(error).__name__)
            raise


def observed(name: str, kind: str = "chain") -> Callable:
    def decorate(function: Callable) -> Callable:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(name, kind):
                return function(*args, **kwargs)

        return wrapper

    return decorate


def bind_context(function: Callable[..., _T]) -> Callable[..., _T]:
    """Capture the caller; each pool task enters its own copy, including OTel."""
    context = copy_context()

    @wraps(function)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        return context.copy().run(function, *args, **kwargs)

    return wrapper


@contextmanager
def digest_run(execution_mode: str, dry_run: bool) -> Iterator[Observation]:
    # Bound cardinality: caller-provided values never become arbitrary labels.
    mode = execution_mode if execution_mode in {"cli", "web", "scheduled"} else "manual"
    token = _context.set(
        {"execution_mode": mode, "session.id": "condenseit-" + uuid.uuid4().hex}
    )
    runtime = get_runtime()
    started = time.monotonic()
    outcome = "error"
    try:
        with span(
            "condenseit.digest", "agent", {"digest.dry_run": dry_run}
        ) as observation:
            if runtime:
                runtime.emit("condenseit.digest.started")
            observation.content("input", {"dry_run": dry_run})
            try:
                yield observation
                outcome = "success"
            except BaseException as error:
                if runtime:
                    runtime.emit(
                        "condenseit.digest.failed", {"error.type": type(error).__name__}
                    )
                raise
    finally:
        if runtime:
            dimensions = {
                "execution_mode": mode,
                "outcome": outcome,
                "digest.dry_run": dry_run,
            }
            runtime.runs.add(1, dimensions)
            runtime.duration.record(time.monotonic() - started, dimensions)
            if outcome == "success" and not dry_run:
                runtime.last_success = time.time()
            runtime.flush()
        _context.reset(token)


def finish_run(observation: Observation, result: dict[str, Any]) -> None:
    observation.content("output", result)
    if observation.runtime:
        observation.runtime.emit(
            "condenseit.digest.completed", {"digest.id": result.get("digest_id") or 0}
        )


@contextmanager
def model_call(
    model: str, messages: list[dict[str, str]], max_tokens: int
) -> Iterator[Observation]:
    runtime = get_runtime()
    started = time.monotonic()
    outcome = "error"
    attributes = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "openai",
        "gen_ai.request.model": model,
        "gen_ai.request.max_tokens": max_tokens,
        "langfuse.observation.model.name": model,
    }
    with span("chat " + model, "generation", attributes) as observation:
        observation.content("input", messages)
        try:
            yield observation
            outcome = "success"
        finally:
            if runtime:
                dimensions = {
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": model,
                    "outcome": outcome,
                }
                elapsed = time.monotonic() - started
                runtime.calls.add(1, dimensions)
                runtime.latency.record(elapsed, dimensions)
                runtime.emit(
                    "condenseit.model.completed"
                    if outcome == "success"
                    else "condenseit.model.failed",
                    {
                        **dimensions,
                        "duration": elapsed,
                        "gen_ai.response.model": observation.model,
                        **{
                            "gen_ai.usage." + key: value
                            for key, value in observation.usage.items()
                        },
                    },
                )
