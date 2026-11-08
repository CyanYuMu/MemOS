"""Orchestration tests use explicit synthetic boundary implementations."""

import json

from pathlib import Path

import pytest

from memos.skill_drafting import pipeline as module


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    calls = []
    packet = {
        "packet_id": "packet",
        "sources": ["success", "failure"],
        "independent_lineages": ["a", "b"],
    }
    proposal = {"title": "synthetic candidate"}
    generation = {"provider": "synthetic", "requested_model": "fixed-model"}
    candidate = {"skill_id": "test-skill", "revision": "revision", "status": "candidate"}

    def collect(bridge, plan):
        calls.append("collect")
        return packet

    class Generator:
        def generate(self, value):
            assert value is packet
            calls.append("generate")
            return proposal, generation

    def make(value, inputs, *, skill_id, generation):
        assert value is proposal and inputs is packet and skill_id == "test-skill"
        calls.append("validate")
        return candidate

    def recheck(bridge, value):
        assert value is packet
        calls.append("recheck")

    def write(destination, inputs, value, result, metadata):
        calls.append("write")
        destination.mkdir()
        path = destination / "manifest.json"
        path.write_text(json.dumps({"candidate": candidate}))
        return path

    monkeypatch.setattr(module, "collect_packet", collect)
    monkeypatch.setattr(module, "make_candidate", make)
    monkeypatch.setattr(module, "recheck_packet", recheck)
    monkeypatch.setattr(module, "write_candidate", write)
    return {
        "calls": calls,
        "packet": packet,
        "proposal": proposal,
        "generation": generation,
        "generator": Generator(),
        "output": tmp_path / "job",
    }


def test_collect_generate_revalidate_then_seal(scenario):
    s = scenario
    path = module.draft(
        object(), {"plan": "fixed"}, s["generator"], s["output"], skill_id="test-skill"
    )
    result = json.loads(path.read_text())
    assert s["calls"] == ["collect", "generate", "validate", "recheck", "write"]
    assert result["status"] == "completed"
    assert Path(result["candidate_directory"]) == s["output"] / "candidate"
    assert result["qualification"] == "unpublished_candidate"
    assert (s["output"] / "packet.json").is_file()


def test_missing_source_prevents_model_call(scenario, monkeypatch):
    def unavailable(*args):
        raise ValueError("private source endpoint")

    monkeypatch.setattr(module, "collect_packet", unavailable)
    s = scenario
    result = json.loads(
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill").read_text()
    )
    assert result["status"] == "failed" and result["failure_stage"] == "collect"
    assert s["calls"] == []
    assert not (s["output"] / "candidate").exists()
    assert "private source endpoint" not in (s["output"] / "job.json").read_text()


def test_changed_source_retains_proposal_but_not_success(scenario, monkeypatch):
    def changed(*args):
        raise ValueError("source changed")

    monkeypatch.setattr(module, "recheck_packet", changed)
    s = scenario
    result = json.loads(
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill").read_text()
    )
    assert result["status"] == "failed" and result["failure_stage"] == "recheck"
    assert json.loads((s["output"] / "proposal.json").read_text()) == s["proposal"]
    assert not (s["output"] / "candidate").exists()
    assert "write" not in s["calls"]


def test_existing_directory_does_not_repeat_model_call(scenario):
    s = scenario
    module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill")
    with pytest.raises(ValueError, match="draft_output_exists"):
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill")
    assert s["calls"].count("generate") == 1


def test_private_model_metadata_is_not_persisted(scenario):
    s = scenario
    s["generation"]["reasoning_content"] = "private reasoning payload"
    result = json.loads(
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill").read_text()
    )
    assert result["status"] == "failed"
    assert not (s["output"] / "generation.json").exists()
    assert "private reasoning payload" not in "".join(
        p.read_text() for p in s["output"].glob("*.json")
    )


def test_model_failure_keeps_input_without_retry(scenario):
    s = scenario
    count = []

    def fail(_):
        count.append(1)
        raise RuntimeError("private provider credential")

    s["generator"].generate = fail
    result = json.loads(
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill").read_text()
    )
    assert result["status"] == "failed" and result["failure_stage"] == "generate"
    assert count == [1]
    assert (s["output"] / "packet.json").exists()
    assert "private provider credential" not in (s["output"] / "job.json").read_text()


def test_invalid_skill_identity_prevents_collection_and_model_call(scenario):
    s = scenario
    with pytest.raises(ValueError, match="draft_invalid_skill_id"):
        module.draft(None, {}, s["generator"], s["output"], skill_id="not/a/skill")
    assert s["calls"] == []
    assert not s["output"].exists()


def test_public_model_metadata_connects_to_real_domain_and_artifacts(tmp_path, monkeypatch):
    from test_domain import sample_packet, sample_proposal
    from test_generation import Client, Response

    from memos.skill_drafting.artifacts import load_candidate
    from memos.skill_drafting.generation import OpenAIJSONGenerator

    packet = sample_packet()
    proposal = sample_proposal(packet)
    client = Client(Response(json.dumps(proposal)))
    generator = OpenAIJSONGenerator(
        model="fixed-model",
        api_key="synthetic-private-key",
        base_url="https://provider.invalid/v1",
        client=client,
    )
    monkeypatch.setattr(module, "collect_packet", lambda bridge, plan: packet)
    monkeypatch.setattr(module, "recheck_packet", lambda bridge, value: None)
    job_path = module.draft(
        None, packet["plan"], generator, tmp_path / "job", skill_id="contract-repair"
    )
    job = json.loads(job_path.read_text())
    assert job["status"] == "completed", job
    loaded = load_candidate(Path(job["candidate_directory"]))
    assert loaded["proposal"] == proposal
    assert loaded["generation"]["requested_model"] == "fixed-model"
    assert loaded["validation"]["semantic_review"] == "pending"
    assert loaded["validation"]["independent_validation"] == "not_run"
    assert len(client.calls) == 1


def failure_diagnostic():
    return {
        "status": "failed",
        "requested_model": "fixed-model",
        "reported_model": "fixed-model",
        "finish_reason": "length",
        "usage": {"completion_tokens": 16000},
        "usage_status": "reported",
        "parameters": {
            "timeout_seconds": 120,
            "max_tokens": 16000,
            "temperature": 0,
            "max_retries": 0,
            "tools": [],
        },
        "prompt_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
        "elapsed_seconds": 96.0,
    }


def test_failed_job_preserves_safe_generation_diagnostics(scenario):
    from memos.skill_drafting.generation import GenerationError

    s = scenario
    diagnostic = failure_diagnostic()

    def fail(_):
        raise GenerationError("generation_incomplete_response", diagnostic=diagnostic)

    s["generator"].generate = fail
    result = json.loads(
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill").read_text()
    )
    assert result["status"] == "failed"
    assert result["generation_diagnostic"] == diagnostic
    assert result["generation_diagnostic_status"] == "recorded"
    assert not (s["output"] / "candidate").exists()
    assert not (s["output"] / "generation.json").exists()


def test_failed_job_drops_diagnostic_mutated_to_include_raw_response(scenario):
    from memos.skill_drafting.generation import GenerationError

    s = scenario
    failure = GenerationError("generation_incomplete_response", diagnostic=failure_diagnostic())
    failure.diagnostic["raw_response"] = "private response payload"

    def fail(_):
        raise failure

    s["generator"].generate = fail
    result = json.loads(
        module.draft(None, {}, s["generator"], s["output"], skill_id="test-skill").read_text()
    )
    assert result["status"] == "failed" and result["generation_diagnostic_status"] == "rejected"
    assert "generation_diagnostic" not in result
    assert "private response payload" not in (s["output"] / "job.json").read_text()
