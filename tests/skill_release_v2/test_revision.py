"""Synthetic generation attempts keep source drift and failed evidence visible."""

import importlib
import json

import pytest

from test_generation import API_KEY, module, prepared

from memos.skill_release_v2.artifacts import load_candidate
from memos.skill_release_v2.authority import CONTRACT_PATH


def revision():
    return importlib.import_module("memos.skill_release_v2.revision")


def run(tmp_path, item, generator, *, source_recheck=lambda _: None, output=None):
    return revision().generate_revision(
        tmp_path / "parent",
        output or tmp_path / "attempt",
        generator,
        **{
            key: item[key]
            for key in ("contexts", "input_manifest", "exposure_manifest", "authority_bindings")
        },
        source_recheck=source_recheck,
    )


def test_attempt_preserves_public_response_and_seals_a_reloaded_candidate(tmp_path):
    item, generator, client, _ = prepared(tmp_path)
    checks = []

    def check(packet):
        checks.append(packet["packet_id"])
        packet.clear()  # 回读能力不能改写后续使用的已冻结来源。

    def observe_start():
        started = json.loads((tmp_path / "attempt" / "request-started.json").read_text())
        assert started["status"] == "unknown"
        assert started["input_manifest_id"] == item["input_manifest"]["input_manifest_id"]

    client.effect = observe_start
    result = run(tmp_path, item, generator, source_recheck=check)
    assert result == tmp_path / "attempt" / "candidate" / "manifest.json"
    loaded = load_candidate(result.parent, authority_bindings=item["authority_bindings"])
    assert loaded["proposal"] == item["proposal"]
    assert loaded["generation"]["model_identity_match"] is False
    assert len(list(result.parent.iterdir())) == 11
    assert len(checks) >= 4
    assert (
        json.loads((result.parent.parent / "generation.json").read_text()) == loaded["generation"]
    )
    assert len(client.calls) == client.closed == 1


@pytest.mark.parametrize("phase", ["pre", "post", "seal_before", "seal_after"])
@pytest.mark.parametrize("source", ["memory", "parent", "context", "producer"])
def test_each_source_is_rechecked_before_after_and_at_sealing(tmp_path, monkeypatch, phase, source):
    item, generator, client, _ = prepared(tmp_path)
    checks = []
    threshold = {"pre": 1, "post": 2, "seal_before": 3, "seal_after": 4}[phase]
    actual = module().current_producer()

    def check(_):
        checks.append(True)
        if len(checks) == threshold:
            if source == "memory":
                raise ValueError("private source failure " + API_KEY)
            if source == "parent":
                path = tmp_path / "parent" / "proposal.json"
                path.write_bytes(path.read_bytes() + b" ")
            if source == "context":
                path = next(iter(item["authority_bindings"].values())) / CONTRACT_PATH
                path.write_bytes(path.read_bytes() + b"\n# drift\n")
            if source == "producer":
                changed = {
                    "version": actual["version"],
                    "files": {**actual["files"], "generation.py": "a" * 64},
                }
                monkeypatch.setattr(module(), "current_producer", lambda: changed)

    result = run(tmp_path, item, generator, source_recheck=check)
    assert result.name == "failure.json"
    diagnostic = json.loads(result.read_text())
    assert diagnostic["request_attempts"] == (0 if phase == "pre" else 1)
    assert not (result.parent / "candidate").exists()
    assert not list(result.parent.glob(".candidate*"))
    assert API_KEY not in result.read_text()
    if phase != "pre":
        assert json.loads((result.parent / "proposal.json").read_text()) == item["proposal"]
        assert (result.parent / "generation.json").exists()
        assert len(client.calls) == 1
    else:
        assert client.calls == []


def test_public_but_structurally_invalid_proposal_is_retained_before_rejection(tmp_path):
    item, generator, client, _ = prepared(tmp_path)
    client.response.choices[0].message.content = '{"public":"complete but invalid proposal"}'
    result = run(tmp_path, item, generator)
    assert result.name == "failure.json"
    assert json.loads((result.parent / "proposal.json").read_text()) == {
        "public": "complete but invalid proposal"
    }
    assert (result.parent / "generation.json").exists()
    assert not (result.parent / "candidate").exists()


@pytest.mark.parametrize("kind", ["private", "incomplete", "provider_exception"])
def test_private_or_incomplete_responses_and_provider_errors_never_persist_content(tmp_path, kind):
    item, generator, client, _ = prepared(tmp_path)
    if kind == "private":
        client.response.choices[0].message.content = json.dumps({"x": API_KEY})
    elif kind == "incomplete":
        client.response.choices[0].finish_reason = "length"
    else:

        def fail():
            raise RuntimeError(API_KEY)

        client.effect = fail
    result = run(tmp_path, item, generator)
    assert result.name == "failure.json"
    assert not (result.parent / "proposal.json").exists()
    assert not (result.parent / "generation.json").exists()
    assert not (result.parent / "candidate").exists()
    for path in result.parent.iterdir():
        assert API_KEY not in path.read_text()


def test_postcheck_failure_does_not_replace_the_primary_provider_failure(tmp_path):
    item, generator, client, _ = prepared(tmp_path)
    checks = []

    def check(_):
        checks.append(True)
        if len(checks) > 1:
            raise RuntimeError("secondary source drift " + API_KEY)

    def fail():
        raise TimeoutError(API_KEY)

    client.effect = fail
    result = run(tmp_path, item, generator, source_recheck=check)
    primary = json.loads(result.read_text())
    assert primary["code"] == "generation_v2_request_failed"
    secondary = json.loads((result.parent / "postcheck-failure.json").read_text())
    assert secondary["stage"] == "postcheck"
    assert len(checks) == 2


@pytest.mark.parametrize("kind", ["existing", "symlink", "ancestor_symlink", "started_only"])
def test_existing_or_unsafe_attempt_cannot_be_replayed(tmp_path, kind):
    item, generator, client, _ = prepared(tmp_path)
    output = tmp_path / "attempt"
    if kind in {"existing", "started_only"}:
        output.mkdir()
        if kind == "started_only":
            (output / "request-started.json").write_text('{"status":"unknown"}')
    else:
        target = tmp_path / "other"
        target.mkdir()
        output.symlink_to(target, target_is_directory=True)
        if kind == "ancestor_symlink":
            output /= "child"
    with pytest.raises(module().GenerationError):
        run(tmp_path, item, generator, output=output)
    assert client.calls == []


def test_request_marker_fsync_failure_prevents_provider_call(tmp_path, monkeypatch):
    item, generator, client, _ = prepared(tmp_path)

    def fail(_):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(revision().os, "fsync", fail)
    with pytest.raises(module().GenerationError):
        run(tmp_path, item, generator)
    assert client.calls == []


def test_caller_mutation_during_provider_call_cannot_rewrite_frozen_material(tmp_path):
    item, generator, client, _ = prepared(tmp_path)
    context_id = item["contexts"][0]["context_id"]

    def mutate():
        item["contexts"].clear()
        item["input_manifest"].clear()

    client.effect = mutate
    result = run(tmp_path, item, generator)
    loaded = load_candidate(result.parent, authority_bindings=item["authority_bindings"])
    assert loaded["candidate"]["contract_context_ids"] == [context_id]


def test_close_failure_still_preserves_complete_public_response_without_sealing(tmp_path):
    item, generator, client, _ = prepared(tmp_path)

    def close():
        client.closed += 1
        raise OSError(API_KEY)

    client.close = close
    result = run(tmp_path, item, generator)
    assert json.loads(result.read_text())["code"] == "generation_v2_client_close_failed"
    assert json.loads((result.parent / "proposal.json").read_text()) == item["proposal"]
    assert (result.parent / "generation.json").exists()
    assert not (result.parent / "candidate").exists()


def test_secondary_failure_keeps_the_fact_that_a_rejected_response_arrived(tmp_path):
    item, generator, client, _ = prepared(tmp_path)
    client.response.choices[0].finish_reason = "length"
    calls = []

    def check(_):
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("drift")

    result = run(tmp_path, item, generator, source_recheck=check)
    assert json.loads(result.read_text())["provider_response_received"] is True
    secondary = json.loads((result.parent / "postcheck-failure.json").read_text())
    assert secondary["provider_response_received"] is True


def test_actual_request_started_fsync_failure_leaves_zero_provider_calls(tmp_path, monkeypatch):
    item, generator, client, _ = prepared(tmp_path)
    real_write = revision()._write
    real_fsync = revision().os.fsync
    writing_start = False

    def write(path, value, owner):
        nonlocal writing_start
        writing_start = path.name == "request-started.json"
        return real_write(path, value, owner)

    def sync(fd):
        if writing_start:
            raise OSError("synthetic start-marker failure")
        real_fsync(fd)

    monkeypatch.setattr(revision(), "_write", write)
    monkeypatch.setattr(revision().os, "fsync", sync)
    with pytest.raises(module().GenerationError):
        run(tmp_path, item, generator)
    assert not client.calls
    assert (tmp_path / "attempt" / "request-started.json").exists()


def test_concurrent_attempt_directories_are_exclusively_claimed(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    item, first, first_client, _ = prepared(tmp_path)
    second_client = type(first_client)(first_client.response)
    second = module().OpenAIJSONGeneratorV2(
        API_KEY, "https://synthetic-provider.invalid/v1/", client_factory=lambda **_: second_client
    )

    def invoke(generator):
        try:
            return run(tmp_path, item, generator)
        except module().GenerationError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(invoke, [first, second]))
    assert sum(value is not None for value in results) == 1
    assert len(first_client.calls) + len(second_client.calls) == 1


def test_failure_record_storage_error_cannot_mask_primary_provider_error(tmp_path, monkeypatch):
    item, generator, client, _ = prepared(tmp_path)
    real_write = revision()._write
    checks = []

    def timeout():
        raise TimeoutError(API_KEY)

    def write(path, value, owner):
        if path.name == "failure.json":
            raise module().GenerationError("generation_v2_storage_failed")
        return real_write(path, value, owner)

    client.effect = timeout
    monkeypatch.setattr(revision(), "_write", write)
    with pytest.raises(module().GenerationError) as caught:
        run(tmp_path, item, generator, source_recheck=lambda _: checks.append(True))
    assert caught.value.code == "generation_v2_request_failed"
    assert caught.value.diagnostic["code"] == "generation_v2_request_failed"
    assert caught.value.diagnostic["request_attempts"] == 1
    assert caught.value.retention_failed is True
    assert API_KEY not in str(caught.value) + json.dumps(caught.value.diagnostic)
    assert len(client.calls) == client.closed == 1
    assert len(checks) == 2


def test_secondary_diagnostic_storage_error_keeps_existing_primary_path(
    tmp_path, monkeypatch, caplog
):
    item, generator, client, _ = prepared(tmp_path)
    real_write = revision()._write
    checks = []

    def timeout():
        raise TimeoutError(API_KEY)

    def check(_):
        checks.append(True)
        if len(checks) == 2:
            raise ValueError(API_KEY)

    def write(path, value, owner):
        if path.name == "postcheck-failure.json":
            raise module().GenerationError("generation_v2_storage_failed")
        return real_write(path, value, owner)

    client.effect = timeout
    monkeypatch.setattr(revision(), "_write", write)
    result = run(tmp_path, item, generator, source_recheck=check)
    assert result.name == "failure.json"
    assert json.loads(result.read_text())["code"] == "generation_v2_request_failed"
    assert "generation_v2_secondary_retention_failed" in caplog.text
    assert API_KEY not in caplog.text
    assert not (result.parent / "candidate").exists()
