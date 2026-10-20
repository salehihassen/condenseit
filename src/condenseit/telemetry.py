"""Opt-in OTLP request traces; providers stay local and disabled calls need no SDK."""

import atexit
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps

logger = logging.getLogger(__name__)
_session = ContextVar("digest_session", default="")
_lock = threading.Lock()
_provider = None
_configured = False


def get_runtime():
    global _provider, _configured
    if (
        os.getenv("CONDENSEIT_OTEL_ENABLED", "").lower() != "true"
        or os.getenv("OTEL_SDK_DISABLED", "").lower() == "true"
    ):
        return None
    with _lock:
        if not _configured:
            _configured = True
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
                from opentelemetry.sdk.resources import Resource
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                resource = Resource.create(
                    {"service.name": os.getenv("OTEL_SERVICE_NAME", "condenseit")}
                )
                exporter = OTLPSpanExporter(timeout=2)
                provider = TracerProvider(resource=resource)
                provider.add_span_processor(
                    BatchSpanProcessor(exporter, max_queue_size=512)
                )
                atexit.register(provider.shutdown)
                _provider = provider
            except Exception as error:
                logger.warning("Telemetry unavailable (%s)", type(error).__name__)
    return _provider


@contextmanager
def _span(name, kind, attributes=None):
    from opentelemetry.trace import StatusCode

    identity = {
        "application": "condenseit",
        "workflow": "digest",
        "session.id": _session.get(),
        "langfuse.session.id": _session.get(),
        "langfuse.observation.type": kind,
        "langfuse.trace.tags": ["application:condenseit"],
        "langfuse.trace.metadata.application": "condenseit",
        "langfuse.environment": os.getenv("CONDENSEIT_OTEL_ENVIRONMENT", "production"),
    }
    tracer = get_runtime().get_tracer("condenseit")
    with tracer.start_as_current_span(
        name,
        attributes=identity | (attributes or {}),
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as error:
            # Raw exception text may contain credentials or prompt content.
            span.set_status(StatusCode.ERROR, type(error).__name__)
            span.set_attribute("error.type", type(error).__name__)
            raise


def digest_run(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        provider = get_runtime()
        if provider is None:
            return function(*args, **kwargs)
        token = _session.set("condenseit-" + uuid.uuid4().hex)
        try:
            with _span("condenseit.digest", "agent") as span:
                result = function(*args, **kwargs)
                span.set_attribute("digest.id", result.get("digest_id") or 0)
                return result
        finally:
            _session.reset(token)
            try:
                provider.force_flush(timeout_millis=2000)
            except Exception:
                logger.warning("Telemetry flush failed")

    return wrapper


def bind_context(function):
    """Copy digest/span context independently into each article worker."""
    context = copy_context()
    return lambda *args, **kwargs: context.copy().run(function, *args, **kwargs)


def _content(span, field, value):
    if os.getenv("CONDENSEIT_OTEL_CAPTURE_CONTENT", "").lower() == "true":
        text = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        span.set_attribute("langfuse.observation." + field, text[:16000])


def model_call(function):
    @wraps(function)
    def wrapper(self, messages, max_tokens=1400):
        if get_runtime() is None:
            return function(self, messages, max_tokens)
        with _span(
            "chat " + self.model,
            "generation",
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "openai",
                "gen_ai.request.model": self.model,
                "gen_ai.request.max_tokens": max_tokens,
                "langfuse.observation.model.name": self.model,
            },
        ) as span:
            _content(span, "input", messages)
            return function(self, messages, max_tokens)

    return wrapper


def record_response(data):
    if get_runtime() is None:
        return
    from opentelemetry.trace import get_current_span

    span = get_current_span()
    model = str(data.get("model") or "")
    if model:
        span.set_attribute("gen_ai.response.model", model)
        span.set_attribute("langfuse.observation.model.name", model)
    _content(span, "output", data.get("choices", []))
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return
    counts = {}
    for source, target in [
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ]:
        value = usage.get(source)
        if type(value) is int and value >= 0:
            counts[target] = value
            span.set_attribute("gen_ai.usage." + target, value)
    if len(counts) == 2:
        details = usage.get("prompt_tokens_details")
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        cached = (
            min(cached, counts["input_tokens"])
            if type(cached) is int and cached >= 0
            else 0
        )
        span.set_attribute(
            "langfuse.observation.usage_details",
            json.dumps(
                {
                    "input": counts["input_tokens"] - cached,
                    "input_cached": cached,
                    "output": counts["output_tokens"],
                    "total": sum(counts.values()),
                }
            ),
        )
