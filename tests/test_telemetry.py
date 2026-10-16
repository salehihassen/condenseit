"""Exercise actual SDK exports, worker context, opt-out and failure behavior."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from condenseit import telemetry
from condenseit.providers.openai_provider import OpenAISummarizer
from condenseit.services.digest_runner import execute_digest
from condenseit.store.database import ContentStore


@pytest.fixture
def exported(monkeypatch):
    resource = Resource.create({"service.name": "condenseit", "host.name": "test-host"})
    traces = TracerProvider(resource=resource)
    spans = InMemorySpanExporter()
    traces.add_span_processor(SimpleSpanProcessor(spans))
    logs = LoggerProvider(resource=resource)
    records = InMemoryLogRecordExporter()
    logs.add_log_record_processor(SimpleLogRecordProcessor(records))
    reader = InMemoryMetricReader()
    metrics = MeterProvider(resource=resource, metric_readers=[reader])
    runtime = telemetry.Runtime(traces, logs, metrics)
    monkeypatch.setenv("CONDENSEIT_OTEL_ENABLED", "true")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr(telemetry, "_runtime", runtime)
    monkeypatch.setattr(telemetry, "_configured", True)
    yield runtime, spans, records, reader
    runtime.shutdown()


def response(usage=None):
    return {
        "model": "returned-model",
        "usage": usage
        if usage is not None
        else {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 30},
            "completion_tokens_details": {"reasoning_tokens": 5},
        },
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "Harmless marker summary.",
                            "tldr": "Marker",
                            "key_takeaways": [],
                        }
                    )
                }
            }
        ],
    }


@pytest.fixture
def model_http(monkeypatch):
    original = httpx.Client
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(reply), **kw),
    )
    return calls


def test_actual_digest_workers_keep_metadata_parentage_and_usage(
    tmp_path, monkeypatch, exported, model_http
):
    runtime, spans, logs, reader = exported
    monkeypatch.setenv("CONDENSEIT_DATA_DIR", str(tmp_path / "data"))
    config = tmp_path / "config.yaml"
    config.write_text("""
model: requested-model
max_articles_per_digest: 2
summarize_workers: 2
balance_digest_categories: false
llm:
  provider: openai
  openai_model: requested-model
  openai_base_url: https://model.invalid/v1
relevance:
  embedding_provider: "off"
  llm_rerank_enabled: false
output:
  path: data/digests
""")
    store = ContentStore()
    store.deduplicate(
        [
            {
                "title": "Astronomy discovery",
                "url": "https://example.com/space",
                "content": "A new space telescope discovers distant planets and stars.",
                "source": "science",
                "category": "Science",
                "published_at": datetime.now(UTC).isoformat(),
                "collected_at": datetime.now(UTC).isoformat(),
            },
            {
                "title": "Transport improvements",
                "url": "https://example.com/transport",
                "content": "The railway opens new routes between cities.",
                "source": "transport",
                "category": "Transport",
                "published_at": datetime.now(UTC).isoformat(),
                "collected_at": datetime.now(UTC).isoformat(),
            },
        ]
    )
    store.close()
    result = execute_digest(str(config), skip_deploy=True, execution_mode="scheduled")
    assert result["stats"]["articles_count"] == 2
    assert len(model_http) == 2
    observations = spans.get_finished_spans()
    root = next(s for s in observations if s.name == "condenseit.digest")
    pipeline = next(s for s in observations if s.name == "digest.pipeline")
    articles = [s for s in observations if s.name == "digest.article"]
    generations = [
        s
        for s in observations
        if s.attributes["langfuse.observation.type"] == "generation"
    ]
    assert len(articles) == len(generations) == 2
    assert pipeline.parent.span_id == root.context.span_id
    assert {s.parent.span_id for s in articles} == {pipeline.context.span_id}
    assert {s.parent.span_id for s in generations} == {
        s.context.span_id for s in articles
    }
    for observation in observations:
        assert observation.context.trace_id == root.context.trace_id
        assert (
            observation.attributes["langfuse.session.id"]
            == root.attributes["session.id"]
        )
        assert (
            observation.attributes["langfuse.trace.metadata.application"]
            == "condenseit"
        )
        assert observation.attributes["langfuse.trace.metadata.workflow"] == "digest"
        assert (
            observation.attributes["langfuse.trace.metadata.execution_mode"]
            == "scheduled"
        )
        assert "application:condenseit" in observation.attributes["langfuse.trace.tags"]
        assert "langfuse.observation.input" not in observation.attributes
        assert "langfuse.observation.output" not in observation.attributes
    for generation in generations:
        attrs = generation.attributes
        assert attrs["gen_ai.request.model"] == "requested-model"
        assert attrs["gen_ai.response.model"] == "returned-model"
        assert json.loads(attrs["langfuse.observation.usage_details"]) == {
            "input": 70,
            "input_cached": 30,
            "output": 20,
            "total": 120,
        }
    events = [r.log_record for r in logs.get_finished_logs()]
    assert {r.body for r in events} == {
        "condenseit.digest.started",
        "condenseit.model.completed",
        "condenseit.digest.completed",
    }
    assert all(r.trace_id == root.context.trace_id for r in events)
    assert runtime.last_success > 0
    metrics = reader.get_metrics_data().resource_metrics[0].scope_metrics[0].metrics
    tokens = next(m for m in metrics if m.name == "condenseit.gen_ai.tokens")
    assert sum(p.value for p in tokens.data.data_points) == 240
    assert all(
        "session.id" not in p.attributes for m in metrics for p in m.data.data_points
    )


def test_concurrent_runs_have_independent_sessions(exported):
    _, spans, _, _ = exported

    def run(_index):
        with telemetry.digest_run("cli", False):
            with ThreadPoolExecutor(max_workers=2) as pool:

                def child(_):
                    with telemetry.span("child"):
                        pass

                list(pool.map(telemetry.bind_context(child), range(3)))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))
    roots = [s for s in spans.get_finished_spans() if s.parent is None]
    assert len(roots) == 2
    assert len({s.attributes["session.id"] for s in roots}) == 2
    for root in roots:
        children = [
            s
            for s in spans.get_finished_spans()
            if s.parent and s.parent.span_id == root.context.span_id
        ]
        assert len(children) == 3
        assert all(
            s.attributes["session.id"] == root.attributes["session.id"]
            for s in children
        )
    assert telemetry._context.get() is None


def test_failure_correlates_logs_without_exporting_exception_secrets(exported):
    _, spans, logs, _ = exported
    with pytest.raises(ValueError, match="private-error-detail"):
        with telemetry.digest_run("web", False):
            with telemetry.model_call("test", [], 20):
                raise ValueError("private-error-detail")
    assert all(s.status.status_code.name == "ERROR" for s in spans.get_finished_spans())
    events = [r.log_record for r in logs.get_finished_logs()]
    failed = [r for r in events if str(r.body).endswith(".failed")]
    assert {r.body for r in failed} == {
        "condenseit.digest.failed",
        "condenseit.model.failed",
    }
    assert len({r.trace_id for r in events}) == 1
    assert failed[0].trace_id != 0
    assert "private-error-detail" not in str(
        [s.to_json() for s in spans.get_finished_spans()]
    )
    assert "private-error-detail" not in str([r.attributes for r in events])


def test_capture_opt_in_is_bounded(exported):
    runtime, spans, _, _ = exported
    runtime.capture = True
    runtime.content_limit = 12
    with telemetry.model_call(
        "test", [{"role": "user", "content": "long marker"}], 20
    ) as call:
        call.response(response())
    attrs = spans.get_finished_spans()[0].attributes
    assert len(attrs["langfuse.observation.input"]) == 12
    assert len(attrs["langfuse.observation.output"]) == 12


@pytest.mark.parametrize(
    "usage",
    [
        "bad usage",
        {"prompt_tokens": -2},
        {"completion_tokens": True},
        {"prompt_tokens_details": "bad details"},
    ],
)
def test_malformed_usage_does_not_break_generation(exported, usage):
    with telemetry.model_call("test", [], 10) as call:
        call.response(response(usage))


def test_disabled_does_not_initialize_sdk(monkeypatch):
    monkeypatch.delenv("CONDENSEIT_OTEL_ENABLED", raising=False)
    with patch.object(
        telemetry, "_configure", side_effect=AssertionError("SDK imported")
    ):
        with telemetry.digest_run("cli", False):
            with telemetry.model_call("test", [], 10):
                pass


def test_initialization_failure_does_not_break_digest(monkeypatch, caplog):
    monkeypatch.setenv("CONDENSEIT_OTEL_ENABLED", "true")
    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.setattr(telemetry, "_runtime", None)
    with patch.object(
        telemetry, "_configure", side_effect=ValueError("private-config")
    ):
        with telemetry.digest_run("cli", False):
            pass
    assert "private-config" not in caplog.text
    assert "Telemetry unavailable" in caplog.text


def test_retry_is_one_logical_generation(exported, monkeypatch):
    _, spans, _, _ = exported
    original = httpx.Client
    count = 0

    def reply(request):
        nonlocal count
        count += 1
        return (
            httpx.Response(429, headers={"Retry-After": "0"})
            if count == 1
            else (httpx.Response(200, json=response()))
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(reply), **kw),
    )
    assert OpenAISummarizer("test", "https://model.invalid/v1")._chat([])
    generations = spans.get_finished_spans()
    assert len(generations) == 1
    assert generations[0].attributes["retry.count"] == 1
    assert generations[0].events[0].name == "model.retry"
