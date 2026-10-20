"""Request tracing through the real provider and digest worker path."""

import json
import sys
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from condenseit import telemetry
from condenseit.providers.openai_provider import OpenAISummarizer
from condenseit.services.digest_runner import execute_digest
from condenseit.store.database import ContentStore


@pytest.fixture
def exported(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setenv("CONDENSEIT_OTEL_ENABLED", "true")
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.delenv("CONDENSEIT_OTEL_CAPTURE_CONTENT", raising=False)
    monkeypatch.setattr(telemetry, "_provider", provider)
    monkeypatch.setattr(telemetry, "_configured", True)
    yield exporter
    provider.shutdown()


@pytest.fixture
def model_http(monkeypatch):
    response = json.loads(r"""
{"model":"returned-model",
 "usage":{"prompt_tokens":100,"completion_tokens":20,
          "prompt_tokens_details":{"cached_tokens":30}},
 "choices":[{"message":{
   "content":"{\"summary\":\"Marker\",\"tldr\":\"Marker\",\"key_takeaways\":[]}"
 }}]}
""")
    original = httpx.Client
    calls = []

    def reply(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: original(
            transport=httpx.MockTransport(reply),
            **kw,
        ),
    )
    return response, calls


def test_digest_workers(tmp_path, monkeypatch, exported, model_http):
    monkeypatch.setenv("CONDENSEIT_DATA_DIR", str(tmp_path / "data"))
    config = tmp_path / "config.yaml"
    config.write_text("""
model: test-model
max_article_age_hours: 0
llm:
  provider: openai
  openai_base_url: https://model.invalid/v1
output:
  path: data/digests
""")
    store = ContentStore()
    store.deduplicate(
        [
            {
                "title": title,
                "url": f"https://example.com/{index}",
                "content": title,
                "collected_at": datetime.now(UTC).isoformat(),
            }
            for index, title in enumerate(
                ["Astronomy discoveries", "Railway improvements"]
            )
        ]
    )
    store.close()
    for _ in range(2):
        result = execute_digest(str(config), skip_deploy=True)
        assert result["stats"]["articles_count"] == 2
    spans = exported.get_finished_spans()
    roots = [s for s in spans if s.name == "condenseit.digest"]
    assert len(roots) == len({s.attributes["session.id"] for s in roots}) == 2
    assert len(model_http[1]) == 4
    for root in roots:
        children = [
            s for s in spans if s.parent and s.parent.span_id == root.context.span_id
        ]
        assert len(children) == 2
        assert root.attributes["digest.id"] > 0
        for child in children:
            assert child.context.trace_id == root.context.trace_id
            assert (
                child.attributes["langfuse.session.id"] == root.attributes["session.id"]
            )
            assert child.attributes["application"] == "condenseit"
            assert child.attributes["gen_ai.response.model"] == "returned-model"
            assert json.loads(
                child.attributes["langfuse.observation.usage_details"]
            ) == {
                "input": 70,
                "input_cached": 30,
                "output": 20,
                "total": 120,
            }
            assert "langfuse.observation.input" not in child.attributes
    assert telemetry._session.get() == ""


@pytest.mark.parametrize(
    "enabled,disabled", [(None, None), ("false", None), ("true", "true")]
)
def test_opt_out_keeps_model_calls_working(monkeypatch, model_http, enabled, disabled):
    for name, value in [
        ("CONDENSEIT_OTEL_ENABLED", enabled),
        ("OTEL_SDK_DISABLED", disabled),
    ]:
        monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv(name, value)
    assert OpenAISummarizer("test", "https://model.invalid/v1")._chat([])
    assert telemetry._provider is None
    assert not telemetry._configured
    assert len(model_http[1]) == 1


def test_capture_is_opt_in_and_bounded(monkeypatch, exported, model_http):
    monkeypatch.setenv("CONDENSEIT_OTEL_CAPTURE_CONTENT", "true")
    model_http[0]["choices"][0]["message"]["content"] = "x" * 20000
    OpenAISummarizer("test", "https://model.invalid/v1")._chat(
        [
            {"role": "user", "content": "y" * 20000},
        ]
    )
    attrs = exported.get_finished_spans()[0].attributes
    assert len(attrs["langfuse.observation.input"]) == 16000
    assert len(attrs["langfuse.observation.output"]) == 16000


def test_errors_exclude_exception_details(exported):
    with patch.object(httpx.Client, "post", side_effect=ValueError("private-error")):
        with pytest.raises(ValueError, match="private-error"):
            OpenAISummarizer("test", "https://model.invalid/v1")._chat([])
    span = exported.get_finished_spans()[0]
    assert span.status.status_code.name == "ERROR"
    assert "private-error" not in span.to_json()


def test_missing_sdk_keeps_model_calls_working(monkeypatch, model_http, caplog):
    monkeypatch.setenv("CONDENSEIT_OTEL_ENABLED", "true")
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_configured", False)
    module = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    with patch.dict(sys.modules, {module: None}):
        assert OpenAISummarizer("test", "https://model.invalid/v1")._chat([])
    assert "Telemetry unavailable" in caplog.text


@pytest.mark.parametrize(
    "usage", ["bad", {"prompt_tokens": -2, "completion_tokens": True}]
)
def test_bad_usage_keeps_model_calls_working(exported, model_http, usage):
    model_http[0]["usage"] = usage
    assert OpenAISummarizer("test", "https://model.invalid/v1")._chat([])
    assert exported.get_finished_spans()
