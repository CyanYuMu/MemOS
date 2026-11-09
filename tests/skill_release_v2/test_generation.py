"""Synthetic clients test the provider boundary; no network is requested."""

import copy
import importlib
import json
import sys
import threading

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from test_inputs import bundle

from memos.skill_drafting.domain import hash_json, json_bytes
from memos.skill_release_v2.candidate import proposal_schema
from memos.skill_release_v2.inputs import build_input_manifest, validate_generation


API_KEY = "synthetic-private-provider-key-123456789"
BASE_URL = "https://synthetic-provider.invalid/v1/"


def module():
    return importlib.import_module("memos.skill_release_v2.generation")


class PublicMessage:
    tool_calls = None
    function_call = None
    refusal = None

    def __init__(self, content):
        self.content = content

    @property
    def reasoning_content(self):
        raise AssertionError("private reasoning must never be read")

    def model_dump(self):
        raise AssertionError("provider objects must never be serialized")


class SyntheticClient:
    def __init__(self, response=None, effect=None):
        self.response = response
        self.effect = effect
        self.calls = []
        self.closed = 0
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        if self.effect:
            self.effect()
        return self.response

    def close(self):
        self.closed += 1


def response(content, *, reported="synthetic-provider-alias", finish="stop", usage=None):
    return SimpleNamespace(
        model=reported,
        choices=[SimpleNamespace(message=PublicMessage(content), finish_reason=finish)],
        usage=usage,
    )


def prepared(tmp_path, *, effect=None):
    api = module()
    item = bundle(tmp_path)
    client = SyntheticClient(response(json_bytes(item["proposal"]).decode()), effect=effect)
    configurations = []

    def factory(**kwargs):
        configurations.append(kwargs)
        return client

    generator = api.OpenAIJSONGeneratorV2(API_KEY, BASE_URL, client_factory=factory)
    old = item["input_manifest"]
    item["input_manifest"] = build_input_manifest(
        item["packet"],
        **{key: item[key] for key in ("contexts", "parent", "exposure_manifest")},
        **{
            key: old[key]
            for key in ("feedback", "host_policy", "requested_model", "parameters", "budget")
        },
        producer=api.current_producer(),
        service_identity=generator.service_identity,
        output_schema=proposal_schema(),
    )
    return item, generator, client, configurations


def generate(generator, item):
    return generator.generate(
        item["packet"],
        **{key: item[key] for key in ("contexts", "parent", "input_manifest", "exposure_manifest")},
    )


def test_exact_request_public_response_and_identity_limit_are_bound(tmp_path):
    item, generator, client, configurations = prepared(tmp_path)
    proposal, record = generate(generator, item)
    manifest = item["input_manifest"]
    assert proposal == item["proposal"]
    validate_generation(record, proposal, input_manifest=manifest)
    assert record["model_identity_match"] is False
    assert record["usage"] == {} and record["usage_status"] == "missing"
    assert client.calls == [
        {
            "model": manifest["requested_model"],
            "messages": manifest["messages"],
            "temperature": 0,
            "max_tokens": 8000,
            "response_format": {"type": "json_object"},
        }
    ]
    assert configurations == [
        {
            "api_key": API_KEY,
            "base_url": BASE_URL.rstrip("/"),
            "timeout": 90,
            "max_retries": 0,
            "follow_redirects": False,
        }
    ]
    assert "synthetic-host-only-baseline" not in json.dumps(client.calls)
    assert "host-only-family" not in json.dumps(client.calls)
    assert client.closed == 1
    with pytest.raises(module().GenerationError, match="already_attempted"):
        generate(generator, item)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "field", ["messages", "schema", "producer", "service", "model", "parameters"]
)
def test_invalid_manifest_or_runtime_identity_never_calls_provider(tmp_path, field):
    item, generator, client, configurations = prepared(tmp_path)
    manifest = item["input_manifest"]
    if field == "messages":
        manifest["messages"][1]["content"] += " injected"
        manifest["prompt_sha256"] = hash_json(manifest["messages"])
    elif field == "schema":
        manifest["output_schema_sha256"] = "a" * 64
    elif field == "producer":
        manifest["producer"]["files"]["generation.py"] = "a" * 64
        manifest["producer_digest"] = hash_json(manifest["producer"])
        from memos.skill_release_v2.inputs import request_identity

        manifest["request_sha256"] = request_identity(manifest)
    elif field == "service":
        manifest["service_identity"] = "a" * 64
        from memos.skill_release_v2.inputs import request_identity

        manifest["request_sha256"] = request_identity(manifest)
    elif field == "model":
        manifest["requested_model"] = "mutated-model"
    else:
        manifest["parameters"]["max_retries"] = 1
    manifest["input_manifest_id"] = hash_json(
        {k: v for k, v in manifest.items() if k != "input_manifest_id"}
    )
    with pytest.raises(module().GenerationError):
        generate(generator, item)
    assert client.calls == configurations == []


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "nan",
        "trailing",
        "list",
        "large",
        "truncated",
        "tools",
        "refusal",
        "private",
        "secret",
        "endpoint",
        "missing_model",
    ],
)
def test_invalid_or_private_responses_close_client_and_only_keep_safe_metadata(tmp_path, kind):
    item, generator, client, _ = prepared(tmp_path)
    value = client.response
    message = value.choices[0].message
    if kind == "duplicate":
        message.content = '{"a":1,"a":2}'
    elif kind == "nan":
        message.content = '{"a":NaN}'
    elif kind == "trailing":
        message.content = "{} {}"
    elif kind == "list":
        message.content = "[]"
    elif kind == "large":
        message.content = json.dumps({"x": "x" * 131072})
    elif kind == "truncated":
        value.choices[0].finish_reason = "length"
    elif kind == "tools":
        message.tool_calls = [SimpleNamespace()]
    elif kind == "refusal":
        message.refusal = "refused"
    elif kind == "private":
        message.content = '{"x":"<analysis>private</analysis>"}'
    elif kind == "secret":
        message.content = json.dumps({"x": API_KEY})
    elif kind == "endpoint":
        message.content = json.dumps({"x": BASE_URL.rstrip("/")})
    else:
        value.model = None
    with pytest.raises(module().GenerationError) as caught:
        generate(generator, item)
    diagnostic = caught.value.diagnostic
    assert diagnostic["request_attempts"] == 1
    assert diagnostic["provider_response_received"] is True
    assert "response_content" not in diagnostic
    assert "messages" not in diagnostic
    assert API_KEY not in json.dumps(diagnostic)
    assert BASE_URL.rstrip("/") not in json.dumps(diagnostic)
    assert len(client.calls) == client.closed == 1


def test_provider_exception_never_leaks_or_retries_and_unknown_usage_is_not_zero(tmp_path):
    def fail():
        raise TimeoutError(API_KEY + BASE_URL)

    item, generator, client, _ = prepared(tmp_path, effect=fail)
    with pytest.raises(module().GenerationError) as caught:
        generate(generator, item)
    diagnostic = caught.value.diagnostic
    assert diagnostic["provider_response_received"] is False
    assert diagnostic["usage"] == {} and diagnostic["usage_status"] == "missing"
    assert diagnostic["request_attempts"] == 1
    assert API_KEY not in str(caught.value) + json.dumps(diagnostic)
    with pytest.raises(module().GenerationError, match="already_attempted"):
        generate(generator, item)
    assert len(client.calls) == client.closed == 1


def test_concurrent_request_is_rejected_without_a_second_provider_attempt(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(3)

    item, generator, client, _ = prepared(tmp_path, effect=block)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(generate, generator, item)
        assert entered.wait(3)
        try:
            with pytest.raises(module().GenerationError, match="busy"):
                generate(generator, item)
        finally:
            release.set()
        first.result()
    assert len(client.calls) == client.closed == 1


@pytest.mark.parametrize(
    "usage, expected, status",
    [
        (None, {}, "missing"),
        (SimpleNamespace(prompt_tokens=-1), {}, "invalid"),
        (
            SimpleNamespace(
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=1),
            ),
            {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5, "reasoning_tokens": 1},
            "reported",
        ),
    ],
)
def test_only_public_numeric_usage_is_retained(tmp_path, usage, expected, status):
    item, generator, client, _ = prepared(tmp_path)
    client.response.usage = usage
    _, record = generate(generator, item)
    assert record["usage"] == expected and record["usage_status"] == status


def test_identity_and_attempt_facts_are_read_only(tmp_path):
    _, generator, _, _ = prepared(tmp_path)
    for name, value in (
        ("service_identity", "a" * 64),
        ("request_attempts", 99),
        ("provider_response_received", True),
    ):
        with pytest.raises(AttributeError):
            setattr(generator, name, value)


def test_producer_drift_during_client_initialization_prevents_request(tmp_path, monkeypatch):
    item, generator, client, _ = prepared(tmp_path)
    changed = copy.deepcopy(item["input_manifest"]["producer"])
    changed["files"]["inputs.py"] = "a" * 64

    def factory(**_):
        monkeypatch.setattr(module(), "current_producer", lambda: changed)
        return client

    generator._factory = factory
    with pytest.raises(module().GenerationError, match="producer_changed") as caught:
        generate(generator, item)
    assert caught.value.diagnostic["request_attempts"] == 0
    assert not client.calls
    assert client.closed == 1


def test_client_factory_failure_is_not_counted_as_a_request(tmp_path):
    item, generator, client, _ = prepared(tmp_path)

    def factory(**_):
        raise ValueError(API_KEY)

    generator._factory = factory
    with pytest.raises(module().GenerationError) as caught:
        generate(generator, item)
    assert caught.value.diagnostic["request_attempts"] == 0
    assert caught.value.diagnostic["provider_response_received"] is False
    assert not client.calls


def test_default_factory_closes_http_client_if_sdk_construction_fails(monkeypatch):
    http_client = SyntheticClient()

    def broken_sdk(**_):
        raise RuntimeError(API_KEY)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=lambda **_: http_client))
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=broken_sdk))
    with pytest.raises(RuntimeError):
        module()._new_client(
            api_key=API_KEY, base_url=BASE_URL, timeout=1, max_retries=0, follow_redirects=False
        )
    assert http_client.closed == 1


@pytest.mark.parametrize("primary_failure", [False, True])
def test_client_close_failure_preserves_original_failure_or_completed_public_result(
    tmp_path, primary_failure
):
    item, generator, client, _ = prepared(tmp_path)

    def close():
        client.closed += 1
        raise OSError(API_KEY)

    client.close = close
    if primary_failure:
        client.response.choices[0].finish_reason = "length"
    with pytest.raises(module().GenerationError) as caught:
        generate(generator, item)
    if primary_failure:
        assert caught.value.code == "generation_v2_incomplete_response"
        assert caught.value.public_result is None
    else:
        assert caught.value.code == "generation_v2_client_close_failed"
        assert caught.value.public_result[0] == item["proposal"]
    assert client.closed == 1
    assert API_KEY not in json.dumps(caught.value.diagnostic)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "file:///tmp/private",
        "https://user:password@example.invalid",
        "https://example.invalid/?key=private",
        "https://example.invalid/#private",
        "https://example.invalid:bad",
        "https://example.invalid/\nprivate",
    ],
)
def test_invalid_endpoint_is_rejected_without_echoing_configuration(url):
    with pytest.raises(module().GenerationError) as caught:
        module().OpenAIJSONGeneratorV2(API_KEY, url)
    assert str(caught.value) == "generation_v2_configuration_invalid"


@pytest.mark.parametrize("content", ['{"x":1e999}', '{"x":"unterminated}', '{"x":"\\ud800"}'])
def test_malformed_json_unicode_and_nonfinite_numbers_are_rejected(tmp_path, content):
    item, generator, client, _ = prepared(tmp_path)
    client.response.choices[0].message.content = content
    with pytest.raises(module().GenerationError):
        generate(generator, item)
    assert client.closed == 1
